import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tas_triage
from abu_decomp.tas_watch import Watcher


class TriageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        (self.project / "src").mkdir()
        (self.project / "include").mkdir()
        (self.project / "src" / "a.CPP").write_text(
            '#include "x.H"\nINCLUDE_ASM("ovl", func_8041CAD8_de);\nvoid func_80209CD8_de(int a)\n{\n}\n'
        )
        (self.project / "src" / "b.CPP").write_text("s32 D_0042F130[4];\n")
        self.profile = SimpleNamespace(source_file=Path("/p/w.json"))
        patch = mock.patch("abu_decomp.tas_triage._project", return_value=self.project)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("abu_decomp.arity.notes", return_value=[])
        self.notes = patch.start()
        self.addCleanup(patch.stop)

    def test_no_c_body_says_input_cannot_help_and_names_recipe_work(self):
        got = tas_triage.claim(self.profile, {"outcome": "stop", "subject": "func_8041CAD8_de"})
        self.assertEqual((got["kind"], got["blocking"]), ("no C body", True))
        self.assertIn("input cannot pass this point", got["cannot_help"])
        self.assertEqual(got["owner"], "func_8041CAD8_de at src/a.CPP:2")
        self.assertEqual(got["next"], "recipe work func_8041CAD8_de")
        self.assertIn("no C body", got["claim"])

    def test_unmapped_data_word_names_the_symbol_site(self):
        got = tas_triage.claim(self.profile, {"outcome": "stop", "subject": "code-pointer-0042f130"})
        self.assertEqual(got["kind"], "unmapped data word")
        self.assertEqual(got["owner"], "D_0042F130 at src/b.CPP:1")

    def test_host_fault_owner_is_the_first_stack_frame(self):
        stack = self.project / "st.txt"
        stack.write_text("#0 func_80209CD8_de (a=1)\n#1 func_8041CAD8_de ()\n")
        got = tas_triage.claim(self.profile, {"outcome": "crash", "subject": "host-fault-080f2e1c"}, [str(stack)])
        self.assertEqual(got["kind"], "host fault")
        self.assertEqual(got["owner"], "func_80209CD8_de at src/a.CPP:3")
        self.assertIn("arity show func_80209CD8_de", got["next"])

    def test_arity_note_is_carried(self):
        self.notes.return_value = [{"function": "func_8041CAD8_de", "next": "x"}]
        got = tas_triage.claim(self.profile, {"outcome": "stop", "subject": "func_8041CAD8_de"})
        self.assertEqual(got["arity"][0]["function"], "func_8041CAD8_de")

    def test_unknown_is_blocking_and_clean_end_is_empty(self):
        got = tas_triage.claim(self.profile, {"outcome": "exit", "subject": "exit 3"})
        self.assertEqual((got["kind"], got["blocking"]), ("unknown", True))
        self.assertEqual(tas_triage.claim(self.profile, {"outcome": "ok"}), {})
        hang = tas_triage.claim(self.profile, {"outcome": "hang"})
        self.assertEqual((hang["kind"], hang["blocking"]), ("hang", True))

    def test_stop_event_carries_the_state_claim(self):
        w = Watcher()
        base = {"run": "/r/1", "polls": 3, "running": False, "stacks": [], "marks": {}, "exit": None}
        w.feed({**base, "run": None, "stopped": {}}, 0)
        (event,) = w.feed(
            {**base, "stopped": {"outcome": "stop", "subject": "s", "kind": "no C body", "next": "n"}}, 1
        )[-1:]
        self.assertEqual((event["event"], event["kind"], event["next"]), ("stop", "no C body", "n"))
