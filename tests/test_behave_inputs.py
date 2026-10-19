import random
import unittest

from abu_decomp.behave import inputs as i
from abu_decomp.behave import prototype as p

PROTO = p.parse_declaration("void f(Actor *a, s32 n, f32 x, u8 flag);", "f", {})
LAYOUT = i.Layout()


class PlanTests(unittest.TestCase):
    def test_same_seed_same_cases_and_another_seed_changes_the_memory(self):
        a, _ = i.seeded_cases(PROTO, 20, 7, LAYOUT)
        b, _ = i.seeded_cases(PROTO, 20, 7, LAYOUT)
        c, _ = i.seeded_cases(PROTO, 20, 8, LAYOUT)
        self.assertEqual(a, b)
        self.assertNotEqual([x["memory"] for x in a], [x["memory"] for x in c])

    def test_baseline_first_cap_respected_and_rows_unique(self):
        cases, regions = i.plan(PROTO, 12, 1, LAYOUT)
        self.assertEqual(len(cases), 12)
        self.assertEqual(cases[0]["labels"], {"a": "region0", "n": "1", "x": "1.0", "flag": "1"})
        rows = [tuple(sorted(c["registers"].items())) for c in cases]
        self.assertEqual(len(set(rows)), len(rows))
        self.assertEqual([r.base for r in regions], [LAYOUT.arena])

    def test_every_argument_is_swept_alone_over_its_edges(self):
        cases, _ = i.plan(PROTO, 64, 1, LAYOUT)
        seen = {c["labels"]["n"] for c in cases}
        self.assertTrue({"0", "-1", "0x7fffffff"} <= seen)
        self.assertIn("0", {c["labels"]["a"] for c in cases})  # a null pointer case
        self.assertTrue(any(c["labels"]["x"] == "-1.0" for c in cases))

    def test_pairwise_covers_every_pair(self):
        domains = [range(3), range(3), range(2)]
        rows = i.pairwise(domains, random.Random(1), 50)
        pairs = {(a, x, b, y) for row in rows for a in range(3) for b in range(a + 1, 3) for x, y in [(row[a], row[b])]}
        want = {(0, x, 1, y) for x in range(3) for y in range(3)}
        self.assertTrue(want <= pairs)
        self.assertLess(len(rows), 3 * 3 * 2)  # fewer rows than the full cross product

    def test_float_arguments_land_in_f12_f14_and_stack_words_in_order(self):
        proto = p.parse_declaration("void g(f32 x, f64 y, s32 a, s32 b, s32 c);", "g", {})
        case = i.plan(proto, 1, 1, LAYOUT)[0][0]
        self.assertEqual(case["registers"]["f12"], 0x3F800000)
        self.assertEqual(case["registers"]["f14"], 0x3FF0000000000000)
        self.assertNotIn(
            "a3", case["registers"]
        )  # the double took a2 and a3 (aligned pair), so a, b, c go on the stack
        self.assertEqual(case["stack"][4:], [1, 1, 1])


class MemoryTests(unittest.TestCase):
    def test_pointer_fields_point_at_their_own_region_and_every_fifth_case_is_null(self):
        learned = i.Learned(hot=((0, ((0x10, 4), (0x14, 4))),), pointers=((0, 0x10),))
        cases, regions = i.seeded_cases(PROTO, 10, 3, LAYOUT, learned)
        self.assertEqual(len(regions), 2)
        child = regions[1].base

        def word(case, offset):
            data = bytes.fromhex(case["memory"][0]["hex"])
            return int.from_bytes(data[offset : offset + 4], "big")

        self.assertEqual(word(cases[0], 0x10), child)
        self.assertEqual(word(cases[4], 0x10), 0)
        self.assertNotEqual(word(cases[1], 0x14), word(cases[2], 0x14))  # hot words are swept

    def test_global_pointers_and_state_become_memory_patches(self):
        learned = i.Learned(pointers=((-1, 0x80101000),), state=(0x80102000,), hot=((0, ((0, 4),)),))
        cases, regions = i.seeded_cases(PROTO, 10, 3, LAYOUT, learned)
        patched = {(m["addr"]) for c in cases for m in c["memory"]}
        self.assertTrue({0x80101000, 0x80102000} <= patched)
        self.assertEqual(regions[-1].base, LAYOUT.region(1).base)

    def test_few_argument_rows_are_repeated_with_other_memory_but_never_as_null_rows(self):
        proto = p.parse_declaration("void h(Actor *a);", "h", {})
        learned = i.Learned(hot=((0, ((0, 4),)),))
        cases, _ = i.seeded_cases(proto, 8, 1, LAYOUT, learned)
        self.assertEqual(len(cases), 8)
        self.assertEqual([c["labels"]["a"] for c in cases].count("0"), 1)
        self.assertEqual(len({c["memory"][0]["hex"] for c in cases}), 8)

    def test_learning_tags_decode_to_the_word_that_held_them(self):
        learned = i.Learned(hot=((0, ((0x20, 4),)),), globals=(0x80103000,), stubs=("get",))
        tags = i.Tags.of(learned)
        self.assertEqual(tags.decode(tags.value("field", (0, 0x20)) + 0x14), ("field", (0, 0x20)))
        self.assertEqual(tags.decode(tags.value("global", 0x80103000) - 4), ("global", 0x80103000))
        self.assertEqual(tags.decode(tags.value("stub", "get")), ("stub", "get"))
        self.assertIsNone(tags.decode(0x80A00000))
        cases, _ = i.seeded_cases(PROTO, 6, 1, LAYOUT, learned, learn=True)
        self.assertEqual(
            int.from_bytes(bytes.fromhex(cases[0]["memory"][0]["hex"])[0x20:0x24], "big"),
            tags.value("field", (0, 0x20)),
        )
        self.assertTrue(any(m["addr"] == 0x80103000 for m in cases[0]["memory"]))
        self.assertEqual(cases[0]["stub_v0"], {"get": tags.value("stub", "get")})

    def test_a_stub_that_returned_a_dereferenced_pointer_answers_with_a_region_of_its_own(self):
        learned = i.Learned(hot=((2, ((0, 4),)),), pointers=((0, 0x10),), stubs=("get", "other"), returns=("get",))
        for learn in (False, True):
            cases, regions = i.seeded_cases(PROTO, 10, 3, LAYOUT, learned, learn=learn)
            answer = LAYOUT.region(1 + i.LIMIT).base  # after the argument region and the child room
            self.assertIn(answer, [r.base for r in regions])
            self.assertEqual(cases[0]["stub_v0"]["get"], answer)
            self.assertEqual(cases[4]["stub_v0"]["get"], 0)  # null every fifth case
            self.assertEqual(LAYOUT.reach(regions), LAYOUT.end(i.LIMIT + 2))
        tags = i.Tags.of(learned)
        self.assertEqual(cases[0]["stub_v0"].get("other"), tags.value("stub", "other"))  # still being asked about
        plain, _ = i.seeded_cases(PROTO, 3, 3, LAYOUT, learned)
        self.assertNotIn("other", plain[0]["stub_v0"])


if __name__ == "__main__":
    unittest.main()
