import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tas_symbolize


class SymbolizeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        (self.project / "src").mkdir()
        (self.project / "include").mkdir()
        (self.project / "src" / "a.CPP").write_text(
            "#ifdef NON_MATCHING\nvoid func_80209CD8_de(int a)\n{\n}\n#else\n"
            'INCLUDE_ASM("o", func_80209CD8_de);\n#endif\n'
        )
        self.profile = SimpleNamespace(source_file=Path("/p/w.json"))
        for target, value in (
            ("abu_decomp.tas_triage._project", self.project),
            ("abu_decomp.tas.settings", {"port": "/p/port"}),
            (
                "abu_decomp.tas_symbolize.symbols",
                ([0x80F2D10, 0x80F2E00, 0x80F3000], ["n64c_x", "func_80209CD8_de", "n64c_y"]),
            ),
            ("abu_decomp.tas_symbolize.line_of", None),
        ):
            patcher = (
                mock.patch(target, return_value=value)
                if target.endswith(("_project", "line_of"))
                else mock.patch(target, **{"return_value": value})
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        notes = [
            {"function": "func_80209CD8_de", "verdict": "disagree", "summary": "defined with 1", "next": "arity fix"}
        ]
        patcher = mock.patch("abu_decomp.arity.notes", return_value=notes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fault_pc_is_named_with_offset_evidence_and_arity(self):
        fault = {"eip": "0x080f2e1c", "si_addr": "0x00000010"}
        out = tas_symbolize.claim(
            self.profile, {"outcome": "crash", "subject": "host-fault-00000010", "fault": fault}, []
        )
        self.assertEqual(out["frame"], "func_80209CD8_de+0x1c")
        self.assertEqual(out["evidence"], "C unproven")
        self.assertEqual(out["arity"][0]["function"], "func_80209CD8_de")
        self.assertEqual(out["next"], "arity fix")
        self.assertIn("func_80209CD8_de+0x1c", out["claim"])

    def test_shim_frame_is_shown_but_not_owner(self):
        fault = {"eip": "0x080f3010", "frames": ["0x080f2e1c"]}
        out = tas_symbolize.claim(
            self.profile, {"outcome": "crash", "subject": "host-fault-00000010", "fault": fault}, []
        )
        self.assertEqual(out["frame"], "func_80209CD8_de+0x1c")  # the game caller on the return chain owns it
        self.assertIn("n64c_y+0x10 is a port/shim frame called from func_80209CD8_de+0x1c", out["note"])
        alone = {"eip": "0x080f3010"}
        out = tas_symbolize.claim(
            self.profile, {"outcome": "crash", "subject": "host-fault-00000010", "fault": alone}, []
        )
        self.assertEqual(out["frame"], "n64c_y+0x10")
        self.assertNotIn("evidence", out)

    def test_a_subject_alone_is_no_pc(self):
        out = tas_symbolize.claim(self.profile, {"outcome": "crash", "subject": "host-fault-080f2e1c"}, [])
        self.assertNotIn("frame", out)

    def test_other_stops_pass_through(self):
        out = tas_symbolize.claim(self.profile, {"outcome": "hang"}, [])
        self.assertEqual(out["kind"], "hang")


if __name__ == "__main__":
    unittest.main()


class ParseTests(unittest.TestCase):
    def test_every_function_of_a_file_is_found_not_only_the_first(self):
        from abu_decomp import evidence

        text = "void a(int *x) {\n  if (x) {\n    y();\n  } else z();\n}\n\nvoid b(int *x) {\n  q();\n}\n"
        self.assertEqual(sorted(evidence.parse(text)), ["a", "b"])
