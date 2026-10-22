import unittest

from abu_decomp import tas, tas_rank

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
    {"movie": "a", "outcome": "stop", "subject": "func_1", "kind": "no C body", "next": "recipe work func_1"},
    {"movie": "b", "outcome": "stop", "subject": "func_1", "kind": "no C body", "next": "recipe work func_1"},
    {"movie": "c", "outcome": "crash", "subject": "host-fault-1", "kind": "host fault", "next": "arity show f"},
    {"movie": "d", "outcome": "ok"},
]
OWES = {
    "top": [
        {"function": "func_1", "file": "a.c", "level": "asm", "reached": 9, "next": "recipe work func_1"},
        {"function": "func_2", "file": "b.c", "level": "C unproven", "reached": 4, "next": "recipe work func_2"},
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

    def test_replay_section_defaults_to_the_watched_args_and_no_view(self):
        class P:
            source_file = "p"

        self.assertEqual(tas._replay(P(), None), {"args": None, "view": False})
        self.assertEqual(tas._replay(P(), {"args": ["--window", "64x48"]})["args"], ["--window", "64x48"])


if __name__ == "__main__":
    unittest.main()


class TypeBeforeMatchTests(unittest.TestCase):
    def test_a_function_waiting_for_a_type_shows_the_type_item_and_a_stop_row_does_not_repeat_it(self):
        owes = {
            "top": [
                {
                    "function": "func_2",
                    "file": "b.c",
                    "level": "asm",
                    "reached": 4,
                    "waits": "func_2.arg0",
                    "next": "recipe work func_2.arg0 --kind type",
                }
            ]
        }
        movies = [
            {
                "movie": "a",
                "outcome": "stop",
                "subject": "func_1",
                "kind": "no C body",
                "next": "recipe work func_1.arg0 --kind type",
            }
        ]
        rows = tas_rank.rank(tas_rank.settings({}), {}, movies, owes)
        self.assertEqual([r["kind"] for r in rows], ["no C body", "type before match"])
        self.assertIn("needs the record func_2.arg0 first", rows[1]["error"])
        self.assertEqual(rows[1]["next"], "recipe work func_2.arg0 --kind type")
        again = tas_rank.rank(tas_rank.settings({}), {}, movies, {"top": [{**owes["top"][0], "function": "func_1"}]})
        self.assertEqual([r["kind"] for r in again], ["no C body"])  # the stop row already names func_1


class RerouteTests(unittest.TestCase):
    def test_a_recorded_stop_row_is_rerouted_when_its_function_now_waits_for_a_type(self):
        import json
        import tempfile
        from pathlib import Path

        from abu_decomp import recipe_needs

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".unbake/recipe").mkdir(parents=True)
            (Path(tmp) / ".unbake/recipe/needs.json").write_text(
                json.dumps({"func_1": {"next": "recipe work func_1.arg0 --kind type"}})
            )
            self.assertEqual(
                recipe_needs.reroute(Path(tmp), "recipe work func_1"), "recipe work func_1.arg0 --kind type"
            )
            self.assertEqual(recipe_needs.reroute(Path(tmp), "recipe work func_2"), "recipe work func_2")
            self.assertEqual(recipe_needs.reroute(Path(tmp), "arity show f"), "arity show f")
