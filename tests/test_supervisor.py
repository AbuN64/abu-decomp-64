import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from abu_decomp.cli import main as public_main
from abu_decomp.supervisor.artifact import current, show
from abu_decomp.supervisor.gdb import Bridge, exit_code
from abu_decomp.supervisor.supervisor import Supervisor


class Profile:
    def __init__(self, root):
        self.runtime_root = root / "runtime"
        self.output_root = root / "output"
        self.root = root
        self.events_path = root / "events.jsonl"

    def resolve_path(self, value):
        return Path(value) if Path(value).is_absolute() else self.root / value


class Screen:
    def getmaxyx(self):
        return 30, 100

    def erase(self):
        pass

    def addnstr(self, *_args):
        pass

    def refresh(self):
        pass


class PanesTests(unittest.TestCase):
    def test_supervisor_resolves_one_immutable_release_and_configured_gdb(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Profile(Path(temporary))
            release = profile.runtime_root / "releases/any/commit"
            release.mkdir(parents=True)
            binary = release / "game"
            binary.write_text("#!/bin/sh\nexit 1\n")
            binary.chmod(0o755)
            current_link = release.parent / "current"
            current_link.symlink_to("commit")
            config = {
                "kind": "supervised",
                "release_current": str(current_link),
                "command": ["{release}/game", "--data", "{release}/data"],
                "status_path": "status.json",
                "runs_root": "runs",
                "retry_delay_seconds": 30,
                "watchdog_seconds": 90,
                "debugger": {"executable": "/usr/bin/gdb", "breakpoint": "Failure", "reason_expression": "format"},
            }
            supervisor = Supervisor(profile, "subject", config, Screen())
            run_dir = profile.root / "runs/one"
            run_dir.mkdir(parents=True)
            argv, env, selected = supervisor._command(run_dir)
            assert selected == binary and argv[1:] == ["--data", str(release / "data")]
            assert env["XDG_DATA_HOME"] == str(run_dir / "data")
            debug = supervisor._debug_command(argv, run_dir)
            assert "--breakpoint" in debug and debug[debug.index("--breakpoint") + 1] == "Failure"

    def test_a_windowed_role_gets_its_video_and_audio_environment_on_top_of_the_streams_display(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Profile(Path(temporary))
            release = profile.runtime_root / "releases/any/commit"
            release.mkdir(parents=True)
            (release / "game").write_text("#!/bin/sh\nexit 1\n")
            (release / "game").chmod(0o755)
            (release.parent / "current").symlink_to("commit")
            config = {
                "kind": "supervised",
                "release_current": str(release.parent / "current"),
                "command": ["{release}/game", "--data-dir", "{release}/data"],
                "env": {"SDL_VIDEODRIVER": "wayland", "SDL_AUDIODRIVER": "pulse", "PULSE_SINK": "stream_sink"},
                "status_path": "status.json",
                "runs_root": "runs",
                "retry_delay_seconds": 30,
                "watchdog_seconds": 90,
            }
            run_dir = profile.root / "runs/one"
            run_dir.mkdir(parents=True)
            with mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/stream"}):
                argv, env, _ = Supervisor(profile, "subject", config, Screen())._command(run_dir)
            assert "--headless" not in argv
            assert (env["SDL_VIDEODRIVER"], env["SDL_AUDIODRIVER"], env["PULSE_SINK"]) == (
                "wayland",
                "pulse",
                "stream_sink",
            )
            assert (env["WAYLAND_DISPLAY"], env["XDG_RUNTIME_DIR"]) == ("wayland-1", "/run/stream")

    def test_without_auto_retry_a_failure_is_held_until_restart_is_requested(self):
        async def scenario(supervisor):
            task = asyncio.create_task(supervisor._countdown())
            await asyncio.sleep(0.05)
            held = not task.done() and supervisor.phase == "held_until_restart"
            supervisor.restart.set()
            return held, await asyncio.wait_for(task, 2)

        with tempfile.TemporaryDirectory() as temporary:
            profile = Profile(Path(temporary))
            config = {
                "status_path": "status.json",
                "runs_root": "runs",
                "retry_delay_seconds": 30,
                "watchdog_seconds": 90,
                "auto_retry": False,
            }
            held, result = asyncio.run(scenario(Supervisor(profile, "subject", config, Screen())))
            assert held and result is False

    def test_cancelled_run_stops_its_output_reader_before_the_log_closes(self):
        # "Task exception was never retrieved ... I/O operation on closed file" in the port-supervisor journal
        async def scenario(supervisor, binary):
            errors = []
            read_output = supervisor._read_output

            async def watched(stream, output):
                try:
                    await read_output(stream, output)
                except ValueError as error:
                    errors.append(error)
                    raise

            supervisor._read_output = watched
            command = ["/bin/sh", "-c", "while :; do echo line; sleep 0.01; done"]
            supervisor._command = lambda _run_dir: (command, dict(os.environ), binary)
            supervisor._debug_command = lambda cmd, _run_dir: cmd
            task = asyncio.create_task(supervisor.run_once())
            await asyncio.sleep(0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.2)  # the child keeps printing; a reader left running would hit the closed log
            supervisor.proc.kill()
            await supervisor.proc.wait()
            return errors

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "binary"
            binary.write_bytes(b"x")
            config = {
                "status_path": "status.json",
                "runs_root": "runs",
                "retry_delay_seconds": 30,
                "watchdog_seconds": 90,
            }
            self.assertEqual(asyncio.run(scenario(Supervisor(Profile(root), "subject", config, Screen()), binary)), [])

    def test_artifact_is_copied_and_verified_from_configured_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Profile(Path(temporary))
            image = profile.root / "source.png"
            image.write_bytes(b"\x89PNG actual result")
            config = {"artifact_root": "artifacts", "metadata_path": "last.json"}
            staged = show(profile, config, image, "Real probe")
            assert current(profile, config)["sha256"] == staged["artifact"]["sha256"]
            assert Path(staged["artifact"]["display_copy"]).read_bytes() == image.read_bytes()

    def test_gdb_bridge_passes_the_shims_deliberate_signals_before_it_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "commands.txt"
            fake = root / "gdb"
            fake.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                f"log = open({str(log)!r}, 'w')\n"
                "for line in sys.stdin:\n"
                "    log.write(line); log.flush()\n"
                "    token = line.split('-', 1)[0]\n"
                "    print(token + '^done', flush=True)\n"
                "    if '-exec-run' in line:\n"
                "        print('*stopped,reason=\"exited-normally\"', flush=True)\n"
                "    if '-gdb-exit' in line:\n"
                "        break\n"
            )
            fake.chmod(0o755)
            read, write = os.pipe()
            self.addCleanup(os.close, write)
            bridge = Bridge(
                ["prog"], gdb_path=fake, library_dir=None, breakpoint="Failure", reason_expression="x", run_dir=root
            )
            with (
                mock.patch.object(sys, "stdin", os.fdopen(read, "rb", 0)),
                redirect_stdout(io.StringIO()),
            ):
                code = asyncio.run(bridge.run())
            self.assertEqual(code, 0)
            sent = log.read_text()
            for name in ("SIGSEGV", "SIGUSR2"):
                self.assertIn(f"handle {name} nostop noprint pass", sent)
                self.assertLess(sent.index(f"handle {name}"), sent.index("-exec-run"))

    def test_gdb_exit_code_reads_the_real_exit_code(self):
        assert exit_code('*stopped,reason="exited",exit-code="01"') == 1

    def test_marker_protocol_is_disabled_until_profile_supplies_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = Profile(root)
            config = {
                "status_path": "status.json",
                "runs_root": "runs",
                "retry_delay_seconds": 30,
                "watchdog_seconds": 90,
            }
            state = Supervisor(profile, "subject", config, Screen())

            async def feed(value):
                reader = asyncio.StreamReader()
                reader.feed_data(value)
                reader.feed_eof()
                await state._read_output(reader, io.StringIO())

            asyncio.run(feed(b"boot-step: Init\nboot-complete: Done\n"))
            self.assertEqual(state.step, "")
            self.assertEqual(state.phase, "starting")
            state.config["step_prefixes"] = ["boot-step:"]
            asyncio.run(feed(b"boot-step: RealInit\n"))
            self.assertEqual(state.step, "RealInit")

    def test_public_artifact_restart_logs_do_not_prepend_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "source.png"
            image.write_bytes(b"\x89PNG actual source")
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "pane-test",
                        "supervisor": {
                            "runtime_root": str(root / "runtime"),
                            "roles": {
                                "result": {
                                    "kind": "artifact",
                                    "artifact_root": str(root / "output/artifacts"),
                                    "metadata_path": str(root / "runtime/last.json"),
                                    "restart_command": ["/bin/sh", "-c", "printf viewer-restarted"],
                                }
                            },
                        },
                    }
                )
            )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = public_main(
                    [
                        "--profile",
                        str(profile_path),
                        "supervisor",
                        "show-artifact",
                        "--role",
                        "result",
                        "--source",
                        str(image),
                        "--name",
                        "real",
                    ]
                )
            self.assertEqual(code, 0)
            envelope = json.loads(out.getvalue())
            self.assertTrue(envelope["ok"])
            self.assertEqual(len(out.getvalue().splitlines()), 1)
            self.assertIn("viewer-restarted", err.getvalue())
