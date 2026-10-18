import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError
from abu_decomp.lanes import Ladder, Lane, overlap_findings

LADDER = """
ladder = ["sol", "astra", "sonnet", "opus"]
[models]
"model-low" = "sol"
"model-mid" = "sonnet"
"model-top" = "opus"
[kind.tooling]
min = "sonnet"
max = "opus"
opus_needs_owner = true
[kind.impl]
min = "sol"
max = "opus"
opus_needs_owner = true
"""


def lane(name, *owns, unit=None, track="official"):
    return Lane(name, "impl", "model-low", track, tuple(owns), unit=unit)


class LaneTests(unittest.TestCase):
    def test_ladder_values_come_from_the_workspace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text(LADDER)
            ladder = Ladder.load(path)
        self.assertEqual(ladder.check("impl", "model-low"), "sol")
        self.assertEqual(ladder.check("tooling", "model-mid"), "sonnet")
        for kind, model in (("tooling", "model-low"), ("tooling", "model-top"), ("impl", "model-top")):
            with self.subTest(kind=kind, model=model), self.assertRaises(DecompError):
                ladder.check(kind, model)
        self.assertEqual(ladder.check("tooling", "model-top", "owner approved 10-18"), "opus")
        with self.assertRaisesRegex(DecompError, "not on the ladder"):
            ladder.check("impl", "unknown")

    def test_overlap_guard_refuses_shared_paths_and_active_units(self):
        live = [lane("OFFICIAL-1", "/work/decomp/src/game"), lane("PORT-1", "/work/port", unit="lane-PORT-1")]
        self.assertEqual(overlap_findings(lane("NEW", "/work/decomp/src/sdk"), live), [])
        experiment = lane("PRODDER", "/work/decomp/src", track="experiment")
        self.assertEqual(
            overlap_findings(experiment, live), ["PRODDER /work/decomp/src overlaps OFFICIAL-1 /work/decomp/src/game"]
        )
        self.assertEqual(
            overlap_findings(lane("X", "/state/x", unit="lane-PORT-1"), live, ["lane-PORT-1"]),
            ["unit lane-PORT-1 is already active"],
        )
        self.assertEqual(overlap_findings(lane("Y"), []), ["Y declares no owned paths"])
        self.assertEqual(overlap_findings(lane("Z", "rel"), []), ["Z owns a relative path rel"])


if __name__ == "__main__":
    unittest.main()
