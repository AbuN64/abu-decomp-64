"""The TAS page's per-function view and coverage maze (module tas_fn), over fixture records and a tiny ELF."""

import json
import struct
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from abu_decomp import claims, tas_fn

SOURCE = """#include "x.h"

s32 func_80000010_de(s32 a)
{
    return a + 1;
}

#ifdef NON_MATCHING
void other_fn(void)
{
}
#else
INCLUDE_ASM("o", other_fn);
#endif
"""


def elf_with(code: bytes, address: int) -> bytes:
    """A big-endian ELF32 with one executable section holding `code` at `address`."""
    header = bytearray(52)
    header[:4] = b"\x7fELF"
    shoff = 52 + len(code)
    struct.pack_into(">I", header, 32, shoff)
    struct.pack_into(">HH", header, 46, 40, 2)
    null = bytes(40)
    text = struct.pack(">IIIIIIIIII", 0, 1, 6, address, 52, len(code), 0, 0, 4, 0)
    return bytes(header) + code + null + text


def jal(target: int) -> bytes:
    return struct.pack(">I", (3 << 26) | ((target & 0x0FFFFFFF) >> 2))


NOP = bytes(4)


class Records(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def record(self, folder, name, **fields):
        path = self.root / folder / "invocations"
        path.mkdir(parents=True, exist_ok=True)
        base = {"invocation": name, "command": "behave", "ok": True, "time": 1000.0, "argv": [], "function": "f"}
        (path / f"{name}.json").write_text(json.dumps({**base, **fields}))

    def test_runs_of_a_function_are_indexed_newest_first_with_their_lane(self):
        self.record("lanes/CLEAN-3", "a", time=10.0, verdict="same")
        self.record(
            "lanes/CLEAN-3", "b", time=20.0, verdict="differs", argv=["behave", "--candidate", "/x/lanes/HAIKU-9/c.CPP"]
        )
        self.record("seat", "c", time=5.0, function="g", argv=["--project", "/home/abu/code/wt/REV1-ASTRA-1"])
        self.record("seat", "d", command="ship")
        index = tas_fn.behave_index([str(self.root / "lanes/*/invocations"), str(self.root / "seat/invocations")])
        self.assertEqual([r["invocation"] for r in index["f"]], ["b", "a"])
        self.assertEqual(index["f"][0]["_lane"], "HAIKU-9")  # the candidate path names the lane first
        self.assertEqual(index["f"][1]["_lane"], "CLEAN-3")
        self.assertEqual(index["g"][0]["_lane"], "REV1-ASTRA-1")
        self.assertNotIn("d", [r["invocation"] for rows in index.values() for r in rows])

    def test_a_record_carries_counts_cases_and_the_candidate_source(self):
        candidate = self.root / "cand.c"
        candidate.write_text(SOURCE.replace("a + 1", "a + 2"))
        result = {
            "mode": "compare",
            "verdict": "differs",
            "function": "func_80000010_de",
            "counts": {"same": 40, "differs": 2, "untestable": 6},
            "compared": 42,
            "total": 48,
            "reason": "case 3: return v0",
            "first_difference": {"case": "c3", "kind": "return", "expected": "0x1", "got": "0x2"},
            "cases_detail": [
                {"name": "c3", "verdict": "differs", "difference": {"kind": "return", "expected": 1, "got": 2}}
            ],
            "candidate": {"file": str(candidate), "function_sha256": "ab"},
        }
        shown = claims.outcome("behave", True, result)
        self.assertEqual(shown["counts"], {"same": 40, "differs": 2, "untestable": 6})
        self.assertEqual(shown["candidate_file"], str(candidate))
        self.assertIn("a + 2", shown["candidate_source"])
        self.assertFalse(shown["passed"])

    def test_candidate_diff_is_against_the_functions_current_main_c(self):
        main = tas_fn.function_text(SOURCE, "func_80000010_de")
        self.assertIn("s32 func_80000010_de(s32 a)", main)
        self.assertNotIn("other_fn", main)
        record = {"candidate_source": SOURCE.replace("a + 1", "a + 2"), "argv": []}
        diff = tas_fn.candidate_diff(record, "func_80000010_de", main)
        self.assertEqual(diff["state"], "diff")
        self.assertIn("-    return a + 1;", diff["diff"])
        self.assertIn("+    return a + 2;", diff["diff"])
        same = tas_fn.candidate_diff({"candidate_source": SOURCE, "argv": []}, "func_80000010_de", main)
        self.assertEqual(same["state"], "same")
        gone = tas_fn.candidate_diff({"candidate_file": "/nonexistent/x.c", "argv": []}, "func_80000010_de", main)
        self.assertEqual(gone["state"], "gone")

    def test_run_view_reads_old_records_without_detail(self):
        old = {"invocation": "i", "verdict": "same", "ok": True, "time": 1.0, "argv": ["behave"], "_lane": "L"}
        view = tas_fn.run_view(old, "f", None)
        self.assertEqual((view["mode"], view["detailed"], view["source"]), ("describe", False, None))
        cand = {"invocation": "j", "argv": ["behave", "--candidate", "/none.c"], "_lane": "L", "verdict": "differs"}
        self.assertEqual(tas_fn.run_view(cand, "f", None)["mode"], "candidate")

    def test_claim_lines_name_the_function_as_a_word(self):
        path = self.root / "CLAIMS.md"
        path.write_text(
            "func_80000010_de LANE-1 12:00\nfunc_80000010_de_x LANE-2\nfunc_80000010_de DROPPED LANE-1 why\n"
        )
        self.assertEqual(len(tas_fn.claim_lines(path, "func_80000010_de")), 2)
        self.assertEqual(tas_fn.claim_lines(None, "x"), [])

    def test_map_rows_state_from_score_and_source(self):
        project = self.root / "p"
        (project / "docs").mkdir(parents=True)
        rows = [
            ["a", 1, 8, 100.0, "src/x/a.CPP", 0],
            ["b", 1, 8, 90.0, "src/x/b.CPP", 0],
            ["c", 1, 8, 100.0, "asm/c.s", 0],
            ["d", 1, 8, 100.0, None, 0],
        ]
        (project / "docs" / "map-v.json").write_text(json.dumps({"fns": rows}))
        got = tas_fn.map_rows(project, "v")
        self.assertEqual([got[k]["state"] for k in "abcd"], ["exact", "fuzzy", "none", "none"])
        self.assertEqual(tas_fn.map_rows(None, "v"), {})


class Graph(unittest.TestCase):
    def test_calls_are_the_jal_targets_that_are_functions(self):
        base = 0x80200000
        code = jal(base + 16) + NOP + jal(base + 16) + NOP + NOP * 5 + jal(base + 0x1000)
        # f at +0 (16 bytes) calls g twice; g at +16 (24 bytes) calls an address outside every function
        with tempfile.TemporaryDirectory() as folder:
            elf = Path(folder) / "t.elf"
            elf.write_bytes(elf_with(code, base))
            calls = tas_fn.elf_calls(elf, {"f": (base, 16), "g": (base + 16, 24)})
        self.assertEqual(calls, {"f": ["g"]})
        self.assertEqual(tas_fn.invert(calls), {"g": ["f"]})

    def test_static_path_climbs_to_a_root_through_live_callers(self):
        callers = {"c": ["b", "x"], "b": ["a"], "x": ["y"]}
        chain = tas_fn.static_path(callers, "c", {"b", "a"})
        self.assertEqual(chain, ["a", "b", "c"])
        self.assertEqual(tas_fn.static_path(callers, "c", set()), ["c"])


def fn_row(name, size, file, port=False, oracle=False, scope="game"):
    return {
        "name": name,
        "size": size,
        "file": file,
        "scope": scope,
        "in_version": True,
        "versions": ["v"],
        "port": {"hit": port, "movie": "m1" if port else None, "count": int(port), "poll": None},
        "oracle": {"hit": oracle, "movie": "m2" if oracle else None, "count": int(oracle), "poll": None},
    }


class Maze(unittest.TestCase):
    TABLE: ClassVar = {
        "generated": 5.0,
        "summary": {"version": "v"},
        "functions": [
            fn_row("a", 16, "src/game/x/1.CPP", port=True),
            fn_row("b", 32, "src/game/x/1.CPP", oracle=True),
            fn_row("c", 8, "src/game/y/2.CPP"),
            fn_row("s", 8, "src/sdk/z.CPP", port=True, scope="sdk"),
        ],
        "runs": [{"movie": "m1", "run": "r1", "covered": [0, 1]}, {"movie": "m2", "run": "r2", "covered": [1]}],
    }
    MAP: ClassVar = {"a": {"state": "exact"}, "b": {"state": "fuzzy"}, "c": {"state": "none"}}

    def test_headline_cells_and_replay_sets(self):
        tiles = [
            {"name": "m1", "start": "power-on", "status": "ok", "polls": 9},
            {"name": "m2", "start": "x", "status": "ok", "polls": 3},
        ]
        maze = tas_fn.maze_view(self.TABLE, self.MAP, tiles)
        self.assertTrue(maze["exact"])
        self.assertEqual((maze["total"], maze["covered"]), (3, 2))  # sdk functions are not game code
        self.assertEqual(maze["covered_pct"], 66.67)
        self.assertEqual(maze["real_c"], 2)  # c has no C
        self.assertEqual(maze["real_pct"], 66.67)
        names = [[c[0] for c in m["cells"]] for m in maze["modules"]]
        self.assertEqual(names, [["a", "b"], ["c"]])
        states = {c[0]: c[2] for m in maze["modules"] for c in m["cells"]}
        self.assertEqual(states, {"a": "exact", "b": "fuzzy", "c": "none"})
        self.assertEqual(maze["replays"][0]["cells"], [0, 1])
        self.assertEqual(maze["replays"][1]["cells"], [1])
        self.assertEqual(maze["replays"][1]["start"], "x")

    def test_a_table_without_per_run_sets_falls_back_to_first_hit(self):
        table = {**self.TABLE, "runs": [{"movie": "m1", "run": "r1"}, {"movie": "m2", "run": "r2"}]}
        maze = tas_fn.maze_view(table, self.MAP, [])
        self.assertFalse(maze["exact"])
        self.assertEqual(maze["replays"][0]["cells"], [0])  # a: port first by m1
        self.assertEqual(maze["replays"][1]["cells"], [1])  # b: oracle first by m2

    def test_module_names_drop_the_src_prefix(self):
        self.assertEqual(tas_fn.module_of("src/game/x/1.CPP"), "game/x")
        self.assertEqual(tas_fn.module_of(""), "(no source)")


if __name__ == "__main__":
    unittest.main()
