import unittest

from abu_decomp import tas_rank

HEALTH = {
    "failed": [
        {"unit": "A/x.CPP", "error": 'src/A/x.CPP:75 #error "define __STDARG_GCC or __STDARG_IDO"'},
        {"unit": "B/y.CPP", "error": "src/B/y.CPP:9 conflicting types for 'f'"},
    ],
    "stale": True,
    "stale_by": "/src/z.c",
    "rebuild": "cmake --build b",
}
LAYERS = [
    {"match": "__STDARG", "layer": "compile flags", "next": "add the define for {unit} ({where}) to the port flags"}
]
MOVIES = [
    {"movie": "a", "outcome": "stop", "subject": "func_1", "kind": "no C body", "next": "decomp arity show func_1"},
    {"movie": "b", "outcome": "stop", "subject": "func_1", "kind": "no C body", "next": "decomp arity show func_1"},
    {"movie": "c", "outcome": "crash", "subject": "host-fault-1", "kind": "host fault", "next": "arity show f"},
    {"movie": "d", "outcome": "ok"},
]
OWES = {
    "top": [
        {"function": "func_1", "file": "a.c", "level": "asm", "reached": 9, "next": "decomp arity show func_1"},
        {"function": "func_2", "file": "b.c", "level": "C unproven", "reached": 4, "next": "decomp arity show func_2"},
    ]
}


class RankTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tas_rank.settings({"layers": LAYERS})
        self.rows = tas_rank.rank(self.cfg, HEALTH, MOVIES, OWES)

    def test_order_is_build_then_stale_then_blocks_by_count_then_owes(self):
        self.assertEqual(
            [r["kind"] for r in self.rows],
            ["build failure", "build failure", "stale binary", "no C body", "host fault", "owes a match"],
        )
        self.assertEqual([r["rank"] for r in self.rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.rows[3]["blocks"], ["a", "b"])
        self.assertEqual(self.rows[5]["where"], "func_2 at b.c")  # func_1 is already a stop row

    def test_build_rows_name_the_owning_layer_and_a_concrete_next(self):
        flags, source = self.rows[0], self.rows[1]
        self.assertEqual((flags["layer"], flags["where"]), ("compile flags", "src/A/x.CPP:75"))
        self.assertEqual(flags["next"], "add the define for A/x.CPP (src/A/x.CPP:75) to the port flags")
        self.assertEqual(source["layer"], "game source")
        self.assertIn("sed -n 6,14p src/B/y.CPP", source["next"])

    def test_render_ends_with_the_first_rows_next(self):
        lines = tas_rank.render(self.rows).splitlines()
        self.assertEqual(lines[-1], "next: add the define for A/x.CPP (src/A/x.CPP:75) to the port flags")
        self.assertEqual(tas_rank.render([]).splitlines()[-1], "next: nothing open")

    def test_rule_order_is_configuration(self):
        cfg = tas_rank.settings({"order": ["owes", "blocks"]})
        self.assertEqual(tas_rank.rank(cfg, HEALTH, MOVIES, OWES)[0]["kind"], "owes a match")


class FaultFrameRankTests(unittest.TestCase):
    def test_crashes_are_ranked_by_their_faulting_frame_not_by_the_accessed_address(self):
        crash = {"outcome": "crash", "kind": "host fault", "next": "arity show f", "frame": "func_80208000_de+0x313"}
        movies = [
            {**crash, "movie": "a", "subject": "host-fault-00000010"},
            {**crash, "movie": "b", "subject": "host-fault-00000014"},
            {"movie": "c", "outcome": "crash", "subject": "host-fault-1", "kind": "host fault", "next": "x"},
        ]
        rows = tas_rank.block_rows(movies)
        found = [(r["where"], r["blocks"]) for r in rows]
        self.assertEqual(found, [("func_80208000_de+0x313", ["a", "b"]), ("host-fault-1", ["c"])])


if __name__ == "__main__":
    unittest.main()
