"""behave/rekey.py: Taste's generated boundaries follow the tree behave acts on (tools mocked)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tools
from abu_decomp.behave import rekey


class RekeyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shared = self.tree("shared", "func_A = 0x80408870")
        self.lane = self.tree("lane", "func_A = 0x80408860")
        self.base = self.root / "behave" / "taste.json"
        self.base.parent.mkdir()
        self.base.write_text(
            json.dumps(
                {
                    "project_root": str(self.shared),
                    "recomp": {
                        "decomp_root": str(self.shared),
                        "gen_root": str(self.root / "gen"),
                        "symbols_file": "versions/{version}/symbol_addrs.txt",
                        "layout_glob": "versions/{version}/*.yaml",
                        "roms": {"v1": f"{self.shared}/roms/base.z64"},
                    },
                    "repro": {"capture": {"facts": str(self.root / "facts.json")}},
                }
            )
        )
        (self.root / "facts.json").write_text(json.dumps({"boot": {"rdram_size": 1}, "entrypoint": 0}))
        self.calls = []
        self.seen = []
        self.profile = SimpleNamespace(
            source_file=self.root / "seat.json",
            section=lambda name: {"taste_profile": str(self.base)},
            path=lambda key, value: Path(value),
        )

    def tree(self, name, symbols):
        path = self.root / name / "versions" / "v1"
        path.mkdir(parents=True)
        (path / "symbol_addrs.txt").write_text(symbols)
        (path / "layout.yaml").write_text("a: 1")
        return self.root / name

    def fake(self, ok=True):
        def invoke(tool, profile, args, **kw):
            self.calls.append(args[:2])
            self.seen.append(json.loads(tools.taste_profile_of(profile).read_text())["project_root"])
            if args[:2] == ["recomp", "generate"]:
                gen = self.root / "gen" / "v1"
                gen.mkdir(parents=True, exist_ok=True)
                (gen / "MANIFEST.json").write_text("{}")
            if args[:2] == ["recomp", "facts"]:
                Path(args[args.index("--output") + 1]).write_text(json.dumps({"entrypoint": 1, "version": "v1"}))
            return SimpleNamespace(ok=ok, returncode=0 if ok else 1, document={"findings": [{"message": "boom"}]})

        return mock.patch.object(rekey.tools, "invoke", invoke)

    def go(self, project, ok=True):
        with self.fake(ok), rekey.rekeyed(self.profile, project, "v1"):
            return tools.taste_profile_of(self.profile)

    def test_new_tree_regenerates_once_from_that_tree(self):
        inside = self.go(self.lane)
        self.assertEqual(self.calls, [["recomp", "generate"], ["recomp", "facts"]])
        self.assertEqual(self.seen, [str(self.lane)] * 2)
        self.assertEqual(json.loads(inside.read_text())["recomp"]["roms"]["v1"], f"{self.lane}/roms/base.z64")
        facts = json.loads((self.root / "facts.json").read_text())
        self.assertEqual(facts, {"boot": {"rdram_size": 1}, "entrypoint": 1, "version": "v1"}, "hand-made keys kept")
        self.go(self.lane)
        self.assertEqual(len(self.calls), 2, "same boundaries: no second regeneration")

    def test_changed_symbols_regenerate_again(self):
        self.go(self.lane)
        (self.lane / "versions/v1/symbol_addrs.txt").write_text("func_A = 0x80408850")
        self.go(self.lane)
        self.assertEqual(len(self.calls), 4)

    def test_shared_tree_uses_the_profile_itself(self):
        self.assertEqual(self.go(self.shared), self.base)
        self.assertEqual(self.seen, [str(self.shared)] * 2)

    def test_failed_generation_refuses_and_is_retried(self):
        with self.assertRaises(rekey.RekeyError):
            self.go(self.lane, ok=False)
        self.go(self.lane)
        self.assertEqual(self.calls.count(["recomp", "generate"]), 2)

    def test_no_taste_profile_is_a_no_op(self):
        with (
            mock.patch.object(rekey.tools, "taste_profile_of", return_value=None),
            rekey.rekeyed(self.profile, self.lane, "v1"),
        ):
            pass
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
