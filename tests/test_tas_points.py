import hashlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError, tas, tas_points
from abu_decomp.config import load_profile

T0 = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
SHA = hashlib.sha1(b"rom").hexdigest()


def _fake_wait(process, timeout, run=None):
    return process.wait(timeout), 1.0


class PointBase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "game.z64").write_bytes(b"rom")
        (self.root / "port").write_text("")
        seat = self.root / "seat.json"
        seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tas": {
                        "movies": "movies",
                        "runs": "runs",
                        "port": "port",
                        "rom": "game.z64",
                        "args": ["--user-dir", "{user_dir}"],
                        "exit_after": 7,
                        "timeout_seconds": 5,
                    },
                }
            )
        )
        self.profile = load_profile(seat)
        self.cfg = tas.settings(self.profile)
        self.movies = self.root / "movies"
        (self.movies / "points").mkdir(parents=True)
        patch = mock.patch.object(tas, "_wait", _fake_wait)
        patch.start()
        self.addCleanup(patch.stop)

    def movie(self, name, body, header="", where="."):
        (self.movies / where).mkdir(exist_ok=True)
        (self.movies / where / f"{name}.tas").write_text(f"# tas 1\n# rom-sha1 {SHA}\n{header}{body}")

    def port(self, seen, recorded="8000 0 0\n"):
        class Port:
            pid = 1
            stdin = io.StringIO()

            def __init__(self, argv, env, stdout, **kwargs):
                seen.append(env)
                Path(env["N64C_TAS_HASH"]).write_text("1 aa\n")
                Path(env["N64C_VIEW"]).write_bytes(b"BM")
                Path(env["N64C_TAS_RECORD"]).write_text(recorded)
                stdout.write("n64c-tas: done polls=1\n")

            def wait(self, timeout=None):
                return 0

        return mock.patch.object(tas.subprocess, "Popen", Port)


class PointTests(PointBase):
    def test_a_movie_with_a_start_is_the_points_polls_then_its_own(self):
        self.movie("menu", "- *3\n", "# area Main menu\n", "points")
        self.movie("go", "- *2\n", "# start menu\n", "points")
        self.movie("m", "A\n", "# start go\n")
        polls = tas.movie_polls(self.cfg, "m")
        self.assertEqual(polls, [(0, 0, 0)] * 5 + [(0x8000, 0, 0)])
        self.assertEqual(tas_points.load(self.cfg, "go")["chain"], ["menu", "go"])
        self.assertEqual(tas.movie_polls(self.cfg, "m", "menu"), [(0, 0, 0)] * 3 + [(0x8000, 0, 0)])  # override

    def test_a_movie_runs_with_its_points_paks_unless_it_names_its_own(self):
        self.movie("p", "- *1\n", "# pak off\n# cpak fresh\n", "points")
        self.movie("a", "A\n", "# start p\n")
        self.movie("b", "A\n", "# start p\n# pak on\n# cpak none\n")
        self.assertEqual((tas.movie_pak(self.cfg, "a"), tas.movie_cpak(self.cfg, "a")), ("off", "fresh"))
        self.assertEqual((tas.movie_pak(self.cfg, "b"), tas.movie_cpak(self.cfg, "b")), ("on", "none"))
        self.assertEqual(tas.movie_cpak(self.cfg, "a", None), "fresh")
        self.assertEqual(tas.movie_pak(self.cfg, "b", "p"), "on")

    def test_chains_that_loop_or_run_long_are_refused(self):
        self.movie("x", "A\n", "# start y\n", "points")
        self.movie("y", "A\n", "# start x\n", "points")
        with self.assertRaises(DecompError) as caught:
            tas_points.load(self.cfg, "x")
        self.assertIn("circular", str(caught.exception))
        with self.assertRaises(DecompError):
            tas_points.load(self.cfg, "missing")
        with self.assertRaises(DecompError):
            tas_points.path(self.cfg, "Bad Name")

    def test_a_point_from_another_rom_is_refused(self):
        (self.movies / "points" / "old.tas").write_text("# tas 1\n# rom-sha1 0000\nA\n")
        with self.assertRaises(DecompError) as caught:
            tas_points.load(self.cfg, "old")
        self.assertIn("recorded on ROM", str(caught.exception))

    def test_save_point_keeps_the_first_polls_and_continues_a_start(self):
        self.movie("menu", "- *3\n", "# area Main menu\n", "points")
        self.movie("long", "A *4\n- *4\n", "# start menu\n")
        first = tas_points.save_point(self.profile, "pressed", movie="long", at=5, area="Options menu", note="a  b")
        self.assertEqual((first["polls"], first["own"], first["start"]), (5, 2, "menu"))
        header, own = tas.decode((self.movies / "points" / "pressed.tas").read_text())
        self.assertEqual(
            (header["start"], header["area"], header["note"], header["cpak"]), ("menu", "Options menu", "a b", "none")
        )
        self.assertEqual(len(own), 2)
        self.assertEqual(len(tas_points.load(self.cfg, "pressed")["polls"]), 5)
        flat = tas_points.save_point(self.profile, "top", movie="menu", at=2)
        self.assertEqual((flat["own"], flat["start"]), (2, None))
        for bad in (0, 99):
            with self.assertRaises(DecompError):
                tas_points.save_point(self.profile, "bad", movie="long", at=bad, replace=True)
        with self.assertRaises(DecompError):
            tas_points.save_point(self.profile, "pressed", movie="long", at=5)  # exists
        with self.assertRaises(DecompError):
            tas_points.save_point(self.profile, "z", movie="long", run="r")  # exactly one source

    def test_play_from_a_point_alone_replays_it_and_a_movie_after_it(self):
        self.movie("menu", "- *3\n", "# area Main menu\n", "points")
        self.movie("go", "A\n")
        seen = []
        with self.port(seen):
            alone = tas.play(self.profile, None, start="menu", now=T0)
            after = tas.play(self.profile, "go", start="menu", now=T0)
        self.assertEqual((alone["movie"], alone["polls"]), ("menu", 3))
        self.assertEqual((after["movie"], after["polls"]), ("go", 4))
        record = tas.read_record(Path(after["run"]))
        self.assertEqual((record["start"], record["prefix"]), ("menu", 3))
        self.assertTrue(Path(seen[1]["N64C_TAS_PLAY"]).read_text().endswith("8000 0 0\n"))
        self.assertIsNone(tas.read_record(Path(alone["run"])).get("start"))

    def test_take_from_a_point_hands_over_at_its_end_and_saves_only_the_new_polls(self):
        self.movie("menu", "- *3\n", "# area Main menu\n# pak off\n", "points")
        seen = []
        with self.port(seen, "0000 0 0\n" * 3 + "8000 0 0\n" * 2):
            result = tas.play(self.profile, None, start="menu", take=True, now=T0)
        env = seen[0]
        self.assertEqual(env["N64C_TAS_TAKE_AT"], "3")
        self.assertEqual(Path(env["N64C_TAS_PLAY"]).read_text(), "0000 0 0\n" * 3)
        record = tas.read_record(Path(result["run"]))
        self.assertEqual(
            (record["input"], record["start"], record["prefix"], record["pak"]), ("human", "menu", 3, "off")
        )
        name = result["saved"]
        self.assertTrue(name.startswith("play-"))
        header, own = tas.decode((self.movies / f"{name}.tas").read_text())
        self.assertEqual((header["start"], header["pak"]), ("menu", "off"))
        self.assertEqual(own, [(0x8000, 0, 0)] * 2)
        self.assertEqual(len(tas.movie_polls(self.cfg, name)), 5)  # replays as point + take
        for bad in ({"name": "x"}, {"source": "r"}):
            with self.assertRaises(DecompError):
                tas.play(self.profile, bad.get("name"), start="menu", take=True, source=bad.get("source"))
        with self.assertRaises(DecompError):
            tas.play(self.profile, None, take=True)

    def test_listing_shows_coverage_of_the_catalogued_areas(self):
        self.movie("menu", "- *3\n", "# area Main menu\n", "points")
        self.movie("opts", "A\n", "# area Options menu\n# start menu\n", "points")
        (self.movies / "points" / "areas.txt").write_text(
            "# kinds\nmenu | Main menu\nmenu | Options menu\narena | Escheria\n"
        )
        with self.port([]):
            tas.play(self.profile, None, start="menu", now=T0)
        listed = tas_points.listing(self.profile)
        self.assertEqual([r["point"] for r in listed["points"]], ["menu", "opts"])
        self.assertEqual([c["name"] for c in listed["covered"]], ["Main menu", "Options menu"])
        self.assertEqual(listed["missing"], [{"kind": "arena", "name": "Escheria"}])
        self.assertEqual(listed["summary"], "2/3 catalogued areas have a start point, 2 points")
        self.assertEqual(listed["points"][0]["port"], "ok")
        self.assertIn("no start point yet: Escheria", tas_points.render(listed))
        self.assertEqual(tas.listing(self.profile)["movies"], [])  # points are not movies

    def test_retrying_a_run_of_a_start_movie_keeps_its_start(self):
        self.movie("menu", "- *2\n", "", "points")
        self.movie("m", "A *3\n", "# start menu\n")
        seen = []
        with self.port(seen, "0000 0 0\n" * 2 + "8000 0 0\n" * 3):
            first = tas.play(self.profile, "m", now=T0)
        found = tas.retry_source(self.cfg, first["id"], 3)
        self.assertEqual((found["start"], found["prefix"], len(found["polls"])), ("menu", 2, 3))


if __name__ == "__main__":
    unittest.main()


class PadTests(PointBase):
    """Movies that drive more than one controller (Multiplayer needs a second pad to leave the weapon screen)."""

    def test_two_pad_movies_round_trip_and_the_raw_form_holds_every_pad(self):
        polls = [(0, 0, 0, 0x8000, 0, 0)] * 2 + [(0x1000, 5, -5, 0, 0, 0)]
        text = tas.encode(polls, {"rom-sha1": SHA})
        self.assertIn("# pads 2", text)
        self.assertIn("- | A *2", text)
        self.assertIn("START 5,-5 | -", text)
        header, back = tas.decode(text)
        self.assertEqual((header["pads"], back), ("2", polls))
        self.assertEqual(tas.parse_raw(tas.format_raw(polls)), polls)
        self.assertEqual(tas.format_raw(polls).splitlines()[0], "0000 0 0 8000 0 0")
        with self.assertRaises(DecompError):
            tas.decode("# tas 1\n# pads 2\nA\n")  # one pad on a two-pad line
        with self.assertRaises(DecompError):
            tas.parse_raw("0000 0 0\n0000 0 0 0000 0 0\n")  # the pad count changes mid-run
        with self.assertRaises(DecompError):
            tas.decode("# tas 1\n# pads 5\nA\n")

    def test_a_movie_after_a_two_pad_point_must_drive_the_same_pads(self):
        self.movie("lobby", "- | -\n", "# pads 2\n# area Multiplayer\n", "points")
        self.movie("one", "A\n")
        self.movie("two", "A | START\n", "# pads 2\n")
        with self.assertRaises(DecompError):
            tas.movie_polls(self.cfg, "one", "lobby")
        self.assertEqual(tas.movie_polls(self.cfg, "two", "lobby"), [(0, 0, 0, 0, 0, 0), (0x8000, 0, 0, 0x1000, 0, 0)])

    def test_play_gives_port_and_oracle_the_pad_count_and_a_take_saves_both_pads(self):
        self.movie("lobby", "- | - *3\n", "# pads 2\n# area Multiplayer\n", "points")
        seen = []
        with self.port(seen, "0000 0 0 0000 0 0\n" * 3 + "0000 0 0 8000 0 0\n"):
            alone = tas.play(self.profile, None, start="lobby", now=T0)
            taken = tas.play(self.profile, None, start="lobby", take=True, now=T0)
        self.assertEqual(seen[0]["N64C_PADS"], "2")
        self.assertEqual(Path(seen[0]["N64C_TAS_PLAY"]).read_text(), "0000 0 0 0000 0 0\n" * 3)
        self.assertEqual(tas.read_record(Path(alone["run"]))["pads"], 2)
        self.assertEqual(seen[1]["N64C_PADS"], "2")
        header, own = tas.decode((self.movies / f"{taken['saved']}.tas").read_text())
        self.assertEqual((header["pads"], header["start"], own), ("2", "lobby", [(0, 0, 0, 0x8000, 0, 0)]))

    def test_a_one_pad_run_sets_no_pad_variable(self):
        self.movie("menu", "- *2\n", "", "points")
        seen = []
        with self.port(seen):
            result = tas.play(self.profile, None, start="menu", now=T0)
        self.assertNotIn("N64C_PADS", seen[0])
        self.assertNotIn("pads", tas.read_record(Path(result["run"])))

    def test_the_oracle_is_told_the_pad_count(self):
        from abu_decomp import tas_oracle

        calls = []

        def fake_popen(argv, env=None, **kw):
            calls.append(env)
            raise OSError("stop here")

        run = self.root / "run"
        run.mkdir()
        oracle = {"host": Path("host"), "env": {}, "shot_every": 25, "timeout_seconds": 5}
        with mock.patch.object(tas_oracle.subprocess, "Popen", fake_popen), self.assertRaises(OSError):
            tas_oracle.start(oracle, run, 5, "on", pads=2)
        self.assertEqual(calls[0]["N64R_PADS"], "2")
