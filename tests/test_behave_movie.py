"""behave/movie.py: the capture's own run from the TAS coverage table (movie, poll, host variables, time limit)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import DecompError
from abu_decomp.behave import movie

ROWS = [
    {
        "name": "Hit",
        "in_version": True,
        "state": "both",
        "oracle": {"hit": True, "movie": "walk", "poll": 200, "count": 40},
    },
    {"name": "Never", "in_version": True, "state": "neither", "oracle": {"hit": False, "movie": None, "poll": None}},
    {"name": "Other", "in_version": False, "state": "neither", "oracle": {"hit": False, "movie": None, "poll": None}},
]
TABLE = {"summary": {"version": "v1", "movies": 7}, "functions": ROWS}


class MovieTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.table = TABLE
        self.card = None
        patches = {
            "settings": mock.patch.object(movie.tas, "settings", return_value={"exit_after": 300}),
            "oracle": mock.patch.object(movie.tas_oracle, "settings", return_value={"env": {}}),
            "table": mock.patch.object(movie.tas_coverage, "read_table", side_effect=lambda cfg: self.table),
            "polls": mock.patch.object(movie.tas, "movie_polls", return_value=[(0, 0, 0), (1, 2, 3)]),
            "pak": mock.patch.object(movie.tas, "movie_pak", return_value="off"),
            "cpak": mock.patch.object(movie.tas, "movie_cpak", return_value="none"),
            "card": mock.patch.object(movie.tas, "start_card", side_effect=lambda cfg, mode: self.card),
        }
        for patch in patches.values():
            patch.start()
            self.addCleanup(patch.stop)

    def choose(self, name, version="v1"):
        return movie.choose(mock.Mock(), self.work, name, version, 0.5)

    def test_a_function_the_table_says_a_movie_reaches_gets_that_movie_and_a_limit_that_follows_the_poll(self):
        own, finding = self.choose("Hit")
        self.assertIsNone(finding)
        self.assertEqual((own["movie"], own["poll"], own["seconds"]), ("walk", 200, movie.MIN_SECONDS + 100))
        self.assertEqual(own["count"], 40)  # the movie's polls and how often it enters the function
        env = own["env"]
        self.assertEqual((env["N64R_PAK"], env["N64R_PADS"]), ("off", "1"))
        self.assertNotIn("N64R_CLOCK", env)  # the logical clock is the oracle's only clock
        self.assertEqual(Path(env["N64R_TAS_PLAY"]).read_text(), "0000 0 0\n0001 2 3\n")
        self.assertNotIn("N64R_CPAK", env)

    def test_the_same_movie_is_stored_once_and_a_card_travels_with_it(self):
        self.card = b"card"
        first, _ = self.choose("Hit")
        again, _ = self.choose("Hit")
        self.assertEqual(first["env"], again["env"])
        self.assertEqual(Path(first["env"]["N64R_CPAK"]).read_bytes(), b"card")
        self.assertEqual(len(list((self.work / "movies").glob("*.raw"))), 1)

    def test_a_function_no_movie_reaches_is_a_named_verdict_not_a_capture(self):
        own, finding = self.choose("Never")
        self.assertIsNone(own)
        self.assertEqual(finding["key"], movie.NO_MOVIE)
        self.assertIn("7 movies", finding["message"])

    def test_what_the_table_cannot_say_leaves_the_fixed_script_alone(self):
        self.assertEqual(self.choose("Unlisted")[1]["key"], movie.NO_MOVIE)
        self.assertEqual(self.choose("Other"), (None, None))  # not a function of this version
        self.assertEqual(self.choose("Never", version="v2"), (None, None))  # the table is for another version
        self.table = None
        self.assertEqual(self.choose("Hit"), (None, None))

    def test_a_movie_that_cannot_be_replayed_is_named_and_not_captured(self):
        with mock.patch.object(movie.tas, "movie_polls", side_effect=DecompError("tas.play", "wrong ROM")):
            own, finding = self.choose("Hit")
        self.assertIsNone(own)
        self.assertEqual(finding["key"], "behave.movie")


if __name__ == "__main__":
    unittest.main()
