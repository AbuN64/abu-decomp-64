import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_seat

SHA_OLD, SHA_NEW = "a" * 40, "b" * 40
SHIM_OLD, SHIM_NEW = "1" * 40, "2" * 40
REQUIRE = {
    "stamp": "port-provenance.json",
    "repo": "https://example.test/Port.git",
    "tree": "/clone",
    "ref": "origin/main",
    "shim_file": "cmake/shim.cmake",
    "shim_pattern": r"dep\(shim \S+ ([0-9a-f]{40})",
}


def git(args, **_):
    out = {"rev-parse": f"{SHA_NEW}\n", "show": f"dep(shim https://example.test/Shim.git {SHIM_NEW}\n    SUBDIR x)"}
    return subprocess.CompletedProcess(args, 0, out[args[3]], "")


class SeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.port = self.dir / "ragewars"
        self.port.write_text("bin")
        self.cfg = {"port": self.port, "seat": dict(REQUIRE)}
        patch = mock.patch("abu_decomp.tas_seat.subprocess.run", side_effect=git)
        patch.start()
        self.addCleanup(patch.stop)

    def stamp(self, **fields):
        (self.dir / "port-provenance.json").write_text(json.dumps({"repo": REQUIRE["repo"], **fields}))

    def test_a_seat_stamped_at_current_main_and_shim_runs(self):
        self.stamp(sha=SHA_NEW, shim=SHIM_NEW)
        tas_seat.check(self.cfg)

    def test_a_stale_seat_is_refused_naming_the_seat_and_both_sets_of_shas(self):
        self.stamp(sha=SHA_OLD, shim=SHIM_OLD)
        with self.assertRaises(DecompError) as raised:
            tas_seat.check(self.cfg)
        text = str(raised.exception)
        for part in (str(self.port), "is stale", SHA_OLD, SHIM_OLD, SHA_NEW, SHIM_NEW, "--port"):
            self.assertIn(part, text)

    def test_a_stale_shim_pin_alone_is_stale(self):
        self.stamp(sha=SHA_NEW, shim=SHIM_OLD)
        with self.assertRaisesRegex(DecompError, "is stale"):
            tas_seat.check(self.cfg)

    def test_a_seat_of_another_repo_is_stale(self):
        (self.dir / "port-provenance.json").write_text(json.dumps({"repo": "other", "sha": SHA_NEW, "shim": SHIM_NEW}))
        with self.assertRaisesRegex(DecompError, "repo other"):
            tas_seat.check(self.cfg)

    def test_a_seat_without_a_stamp_is_refused_as_provenance_less(self):
        with self.assertRaisesRegex(DecompError, f"{self.port} states no provenance .*lacks repo, sha, shim"):
            tas_seat.check(self.cfg)

    def test_a_partial_stamp_names_what_is_missing(self):
        self.stamp(sha=SHA_NEW)
        with self.assertRaisesRegex(DecompError, "lacks shim"):
            tas_seat.check(self.cfg)

    def test_a_shared_port_with_no_requirement_is_refused(self):
        self.cfg["seat"] = {"stamp": "port-provenance.json"}
        with self.assertRaisesRegex(DecompError, "cannot be checked"):
            tas_seat.check(self.cfg)

    def test_no_shared_port_asks_for_port(self):
        with self.assertRaisesRegex(DecompError, "pass --port"):
            tas_seat.check({"port": None, "seat": {}})
        with self.assertRaisesRegex(DecompError, "pass --port"):
            tas.port_of({"port": None})

    def test_the_run_record_states_the_explicit_port_provenance(self):
        self.stamp(sha=SHA_OLD, shim=SHIM_OLD)
        got = tas_seat.record(self.cfg, self.port, "sha1x", explicit=True)
        self.assertEqual(
            got,
            {
                "executable": str(self.port),
                "sha1": "sha1x",
                "seat": False,
                "repo": REQUIRE["repo"],
                "sha": SHA_OLD,
                "shim": SHIM_OLD,
            },
        )

    def test_the_record_of_an_unstamped_candidate_is_empty_not_invented(self):
        got = tas_seat.record({"seat": dict(REQUIRE)}, self.port, "s", explicit=False)
        self.assertEqual((got["repo"], got["sha"], got["shim"], got["seat"]), ("", "", "", True))

    def test_play_refuses_a_stale_seat_before_any_run_and_an_explicit_port_skips_the_check(self):
        self.stamp(sha=SHA_OLD, shim=SHIM_OLD)
        cfg = {**self.cfg, "runs": self.dir / "runs"}
        with (
            mock.patch("abu_decomp.tas.settings", return_value=cfg),
            mock.patch("abu_decomp.tas.reconcile_runs"),
            mock.patch("abu_decomp.tas.prune_plays"),
            mock.patch("abu_decomp.tas.prune_oracle"),
            self.assertRaisesRegex(DecompError, "is stale"),
        ):
            tas.play(mock.MagicMock(), "m")
