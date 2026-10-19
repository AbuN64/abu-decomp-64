"""The run slice: decide whether to re-exec under systemd-run, with every host fact mocked."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from abu_decomp import runslice

IN = "0::/user.slice/user-1000.slice/user@1000.service/decomp.slice/run-r1.scope\n"
OUT = "0::/user.slice/user-1000.slice/session-3.scope\n"


def _done(code: int, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", err)


def _enter(cgroup: str, which: bool = True, probe: Any = None, **extra: Any) -> tuple[bool, list[Any], list[str]]:
    execs: list[Any] = []
    warned: list[str] = []
    result = runslice.enter(
        "decomp.slice",
        ["/venv/bin/python", "-m", "abu_decomp", "make"],
        cgroup=lambda: cgroup,
        which=lambda name: "/usr/bin/systemd-run" if which else None,
        probe=probe or (lambda name: _done(0)),
        execvpe=lambda *call: execs.append(call),
        warn=warned.append,
        **extra,
    )
    return result, execs, warned


class Enter(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != runslice.ENTERED}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_process_already_in_the_slice_runs_in_place(self) -> None:
        self.assertEqual(_enter(IN), (False, [], []))

    def test_a_process_outside_the_slice_re_execs_its_own_argv_under_systemd_run(self) -> None:
        _, execs, warned = _enter(OUT)
        self.assertEqual(warned, [])
        [(program, command, env)] = execs
        self.assertEqual(program, "systemd-run")
        self.assertEqual(
            command,
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--slice=decomp.slice",
                "--",
                "/venv/bin/python",
                "-m",
                "abu_decomp",
                "make",
            ],  # fmt: skip
        )
        self.assertEqual(env[runslice.ENTERED], "decomp.slice")

    def test_a_slice_a_parent_already_tried_is_not_tried_again(self) -> None:
        os.environ[runslice.ENTERED] = "decomp.slice"
        self.assertEqual(_enter(OUT), (False, [], []))

    def test_a_host_without_systemd_run_runs_in_place_with_one_warning(self) -> None:
        result, execs, warned = _enter(OUT, which=False)
        self.assertEqual((result, execs), (False, []))
        self.assertTrue(len(warned) == 1 and "systemd-run is missing" in warned[0])

    def test_a_refusing_systemd_run_runs_in_place_with_one_warning(self) -> None:
        result, execs, warned = _enter(OUT, probe=lambda name: _done(1, "Failed to connect to bus\n"))
        self.assertEqual((result, execs), (False, []))
        self.assertTrue(len(warned) == 1 and "Failed to connect to bus" in warned[0])

    def test_an_exec_that_fails_runs_in_place_with_one_warning(self) -> None:
        def boom(*call: Any) -> None:
            raise OSError("exec format error")

        warned: list[str] = []
        result = runslice.enter(
            "decomp.slice", ["x"], cgroup=lambda: OUT, which=lambda n: "/x", probe=lambda n: _done(0), execvpe=boom,
            warn=warned.append,
        )  # fmt: skip
        self.assertTrue(result is False and len(warned) == 1)


class Names(unittest.TestCase):
    def test_in_slice_matches_a_whole_cgroup_component_only(self) -> None:
        self.assertTrue(runslice.in_slice("decomp.slice", IN))
        self.assertFalse(runslice.in_slice("decomp.slice", OUT))
        self.assertFalse(runslice.in_slice("decomp.slice", "0::/user.slice/notdecomp.slice/x.scope\n"))

    def test_the_slice_comes_from_the_profile_named_first_on_the_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "seat.json"
            profile.write_text(json.dumps({"schema_version": 1, "name": "s", "tools": {"run_slice": "decomp.slice"}}))
            self.assertEqual(runslice.slice_named(["--profile", str(profile), "make"]), "decomp.slice")
            self.assertEqual(runslice.slice_named([f"--profile={profile}", "make"]), "decomp.slice")
            self.assertIsNone(runslice.slice_named(["make"]))
            self.assertIsNone(runslice.slice_named(["--profile", str(Path(tmp) / "absent.json")]))
            profile.write_text(json.dumps({"schema_version": 1, "name": "s", "tools": {}}))
            self.assertIsNone(runslice.slice_named(["--profile", str(profile)]))
            profile.write_text(json.dumps({"schema_version": 1, "name": "s", "tools": {"run_slice": 7}}))
            self.assertIsNone(runslice.slice_named(["--profile", str(profile)]))


if __name__ == "__main__":
    unittest.main()
