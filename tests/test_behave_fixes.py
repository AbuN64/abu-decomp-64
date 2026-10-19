import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import candidate as cand
from abu_decomp.behave import run


class AutoSymbolTests(unittest.TestCase):
    def test_the_builds_generated_link_assignments_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "build" / "v1"
            build.mkdir(parents=True)
            (build / "undefined_funcs_auto.txt").write_text("func_auto_8044ECB0 = 0x8044ECB0;\nbad line\n")
            (build / "undefined_syms_auto.txt").write_text("D_389E = 0x3B6E;\n")
            self.assertEqual(cand.auto_symbols(Path(tmp), "v1"), {"func_auto_8044ECB0": 0x8044ECB0, "D_389E": 0x3B6E})
            self.assertEqual(cand.auto_symbols(Path(tmp), "none"), {})

    def test_auto_symbols_are_provided_unless_the_symbol_table_has_them(self):
        text = cand.linker_script(0x80800000, {"a": [0x80001000, 0, 4, "f"]}, None, {"a": 1, "func_auto_X": 0x80002000})
        self.assertIn("PROVIDE(func_auto_X = 0x80002000);", text)
        self.assertNotIn("PROVIDE(a = 0x00000001)", text)


class BudgetTests(unittest.TestCase):
    def test_only_an_event_budget_stop_counts_as_out_of_budget(self):
        self.assertTrue(run.out_of_budget({"status": "fatal", "detail": "event-budget 0 0 last 0"}))
        self.assertFalse(run.out_of_budget({"status": "fatal", "detail": "syscall 0 0"}))
        self.assertFalse(run.out_of_budget({"status": "ok", "detail": ""}))

    def test_defaults_carry_the_budget_and_depth_cap(self):
        self.assertEqual(run.DEFAULTS["event_budget"], 20000)
        self.assertGreater(run.DEFAULTS["event_budget_cap"], run.DEFAULTS["event_budget"])
        self.assertGreater(run.DEFAULTS["depth_cap"], run.DEFAULTS["depth"])


if __name__ == "__main__":
    unittest.main()
