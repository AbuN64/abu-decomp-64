"""behave/hits.py: further entries of the covering movie as real call states over the first entry's memory."""

import tempfile
import unittest
from pathlib import Path

from abu_decomp.behave import hits, run

SIZE = 1 << 16


def image(directory: Path, name: str, edits: dict[int, bytes]) -> Path:
    data = bytearray(SIZE)
    for at, raw in edits.items():
        data[at : at + len(raw)] = raw
    path = directory / name
    path.write_bytes(bytes(data))
    return path


def seed(path: Path, hit: int, registers: dict[str, str]) -> dict:
    return {
        "capture": {"rdram_path": str(path), "hit": hit},
        "case": {"name": f"seed-F-hit{hit}", "registers": registers},
    }


class ChooseTests(unittest.TestCase):
    def test_few_entries_are_all_taken_and_many_grow_by_forty_percent(self):
        self.assertEqual(hits.choose(4), [2, 3, 4])
        self.assertEqual(hits.choose(1), [])
        picked = hits.choose(100000)
        self.assertEqual(picked[:5], [2, 3, 4, 5, 7])
        self.assertEqual(len(picked), hits.WANTED - 1)
        self.assertEqual(picked, sorted(set(picked)))

    def test_nothing_is_asked_when_one_entry_is_wanted(self):
        self.assertEqual(hits.choose(50, wanted=1), [])

    def test_the_time_limit_follows_the_first_entry_and_the_span(self):
        self.assertEqual(hits.seconds(60, 100, 0.1, span=50), 60 + 15)


class PatchTests(unittest.TestCase):
    def test_runs_close_together_are_one_patch_and_equal_memory_is_not_carried(self):
        base = bytes(SIZE)
        other = bytearray(base)
        other[128:132] = b"\x01\x02\x03\x04"
        other[300:304] = b"\x05\x06\x07\x08"  # within MERGE_GAP of the first run: one patch
        other[20000:20004] = b"\x09\x09\x09\x09"
        rows = hits.patches(base, bytes(other), 0x80000000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["addr"], "0x80000080")
        self.assertEqual(bytes.fromhex(rows[0]["hex"])[:4], b"\x01\x02\x03\x04")
        self.assertLess(sum(len(r["hex"]) // 2 for r in rows), 2048)

    def test_a_hit_that_differs_by_more_than_the_limit_has_no_patches(self):
        self.assertIsNone(hits.patches(bytes(SIZE), bytes([1]) * SIZE, 0x80000000, limit=1024))

    def test_identical_memory_is_no_patch(self):
        self.assertEqual(hits.patches(bytes(SIZE), bytes(SIZE), 0x80000000), [])

    def test_a_long_run_is_cut_into_lines_the_harness_can_read(self):
        rows = hits.patches(bytes(hits.CHUNK * 2 + 10), bytes([7]) * (hits.CHUNK * 2 + 10), 0x80000000, limit=1 << 30)
        self.assertEqual([len(r["hex"]) // 2 for r in rows], [hits.CHUNK, hits.CHUNK, 10])


class RealCasesTests(unittest.TestCase):
    def test_the_first_entry_the_replay_never_reaches_ends_the_list(self):
        first = seed(image(self.dir, "a", {}), 1, {"a0": "0x1"})
        made = {n: seed(image(self.dir, f"i{n}", {n: b"\x01"}), n, {"a0": f"{n:#x}"}) for n in (2, 3)}
        asked = []

        def capture(n):
            asked.append(n)
            return made.get(n, "the oracle ended before the entry")

        cases, notes = hits.real_cases(first, [2, 3, 4, 5, 6, 7], capture, jobs=2)
        self.assertEqual([c["labels"]["hit"] for c in cases], ["2", "3"])
        self.assertEqual(sorted(asked), [2, 3, 4, 5])  # the third group (6, 7) was never asked for
        self.assertEqual(len(notes), 2)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def test_each_entry_is_a_labelled_case_with_its_own_registers_and_patches(self):
        first = seed(image(self.dir, "a", {}), 1, {"a0": "0x80001000"})
        second = seed(image(self.dir, "b", {64: b"\xff\xff\xff\xff"}), 2, {"a0": "0x80002000"})
        cases, notes = hits.real_cases(first, [2], lambda n: second)
        self.assertEqual(notes, [])
        self.assertEqual(cases[0]["labels"], {"seed": "seed-F-hit2", "hit": "2"})
        self.assertEqual(cases[0]["registers"], {"a0": "0x80002000"})
        self.assertEqual(cases[0]["memory"][0]["addr"], "0x80000040")

    def test_an_entry_the_game_made_again_with_the_same_state_is_not_a_second_case(self):
        first = seed(image(self.dir, "a", {}), 1, {"a0": "0x80001000"})
        again = seed(image(self.dir, "b", {}), 2, {"a0": "0x80001000"})
        cases, notes = hits.real_cases(first, [2], lambda n: again)
        self.assertEqual(cases, [])
        self.assertIn("same call state", notes[0])

    def test_entries_that_were_not_captured_or_differ_too_much_are_noted_and_the_rest_kept(self):
        first = seed(image(self.dir, "a", {}), 1, {"a0": "0x1"})
        wide = seed(image(self.dir, "w", {0: bytes([1]) * SIZE}), 2, {"a0": "0x3"})
        good = seed(image(self.dir, "g", {8: b"\x01"}), 3, {"a0": "0x4"})
        answers = {2: wide, 3: good, 4: "the oracle ended before entry 4"}
        old = hits.MAX_PATCH_BYTES
        hits.MAX_PATCH_BYTES = 1024
        self.addCleanup(setattr, hits, "MAX_PATCH_BYTES", old)
        cases, notes = hits.real_cases(first, [2, 3, 4], answers.__getitem__)
        self.assertEqual([c["labels"]["hit"] for c in cases], ["3"])
        self.assertEqual(len(notes), 2)
        self.assertIn("ended before", notes[1])


class SourcesTests(unittest.TestCase):
    def test_real_synthetic_and_untestable_cases_are_counted_apart(self):
        cases = [
            {"labels": {"seed": "s"}},
            {"labels": {"seed": "h2", "hit": "2"}},
            {"labels": {"seed": "s", "arg": "zero"}},
            {"labels": {"x": "region"}},
            {"labels": {"x": "null"}},
        ]
        results = [{"status": s} for s in ("ok", "ok", "ok", "ok", "unmapped")]
        self.assertEqual(
            run.sources(cases, results),
            {
                "real_entries": 2,
                "generated_cases": 0,
                "generated_untestable": 0,
                "real_cases": 2,
                "real_untestable": 0,
                "synthetic_cases": 3,
                "synthetic_untestable": 1,
                "untestable": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
