import copy
import unittest

from abu_decomp.behave import compare as c
from abu_decomp.behave import prototype as p

PROTO = p.parse_declaration("s32 f(Actor *a, s32 n);", "f", {})
CALLEE = p.parse_declaration("void g(f32 y, s32 x);", "g", {})
STACK = (0x803F0000, 0x803F4010)


def call(name, a0=0, a1=0, f12=0):
    return {"k": "call", "name": name, "args": [a0, a1, 0, 0, 0, 0, 0, 0], "f": [f12, 0]}


def case(events=None, net=None, ret=5, status="ok", saved=(), unseeded=(), detail=""):
    return {
        "name": "c0",
        "status": status,
        "detail": detail,
        "saved": list(saved),
        "unseeded": [list(x) for x in unseeded],
        "events": events if events is not None else [call("f"), call("g", 0, 1, 0x3F800000)],
        "net": net if net is not None else [[0x80A00010, 7]],
        "ret": {"v0": ret, "v1": 0, "f0": 0, "f2": 0},
        "reads": [],
    }


def verdict(orig, cand, proto=PROTO, callees=None):
    return c.compare_case(orig, cand, proto, {"g": CALLEE} if callees is None else callees, STACK)


def verdict_opaque(orig, cand, opaque):
    return c.compare_case(orig, cand, PROTO, {"g": CALLEE}, STACK, None, frozenset(opaque))


class OpaqueCalleeTests(unittest.TestCase):
    def test_a_candidate_that_inlines_an_opaque_callee_is_same_when_the_effects_match(self):
        orig = case(events=[call("f"), call("rnc"), call("g", 0, 1, 0x3F800000)])
        cand = case(events=[call("f"), call("g", 0, 1, 0x3F800000)])
        self.assertEqual(verdict_opaque(orig, cand, {"rnc"})["verdict"], c.SAME)
        self.assertEqual(verdict(orig, cand)["verdict"], c.DIFFERS)

    def test_opaque_callees_still_leave_memory_and_return_compared(self):
        orig = case(events=[call("f"), call("rnc")], net=[[0x80A00010, 7]])
        cand = case(events=[call("f")], net=[[0x80A00010, 8]])
        self.assertEqual(verdict_opaque(orig, cand, {"rnc"})["difference"]["kind"], "store")
        self.assertEqual(verdict_opaque(case(ret=1), case(ret=2), {"rnc"})["difference"]["kind"], "return")

    def test_other_calls_keep_their_order_and_arguments(self):
        orig = case(events=[call("f"), call("rnc"), call("g", 0, 1, 5)])
        cand = case(events=[call("f"), call("g", 0, 2, 5)])
        self.assertEqual(verdict_opaque(orig, cand, {"rnc"})["difference"]["kind"], "call-args")


class CompareTests(unittest.TestCase):
    def test_identical_runs_are_same(self):
        self.assertEqual(verdict(case(), case())["verdict"], c.SAME)

    def test_the_candidate_entry_name_and_sibling_prefix_do_not_matter(self):
        cand = case(events=[call("behave_f"), call("behave_g", 0, 1, 0x3F800000)])
        self.assertEqual(verdict(case(), cand, callees={"g": CALLEE})["verdict"], c.SAME)

    def test_return_value_difference_is_named(self):
        got = verdict(case(ret=5), case(ret=6))
        self.assertEqual((got["verdict"], got["difference"]["kind"]), (c.DIFFERS, "return"))
        self.assertIn("original 0x00000005, candidate 0x00000006", got["reason"])

    def test_narrow_return_types_ignore_the_upper_bits_and_unknown_ones_only_note(self):
        small = p.parse_declaration("u8 f(void);", "f", {})
        self.assertEqual(verdict(case(ret=0x1FF), case(ret=0xFF), small)["verdict"], c.SAME)
        word = p.inferred("f")
        got = verdict(case(ret=1), case(ret=2), word)
        self.assertEqual(got["verdict"], c.SAME)
        self.assertTrue(any("return type unknown" in n for n in got["notes"]))

    def test_void_functions_have_no_return_to_compare(self):
        void = p.parse_declaration("void f(void);", "f", {})
        self.assertEqual(verdict(case(ret=1), case(ret=2), void)["verdict"], c.SAME)

    def test_memory_effect_differs_but_the_stack_frame_is_ignored(self):
        got = verdict(case(net=[[0x80A00010, 7]]), case(net=[[0x80A00010, 8]]))
        self.assertEqual(
            (got["verdict"], got["difference"]["kind"], got["difference"]["address"]), (c.DIFFERS, "store", 0x80A00010)
        )
        spill = [[0x803F1000, 1]]
        self.assertEqual(verdict(case(net=[[0x80A00010, 7]]), case(net=[[0x80A00010, 7], *spill]))["verdict"], c.SAME)
        self.assertEqual(verdict(case(net=[]), case(net=[[0x80A00020, 1]]))["verdict"], c.DIFFERS)

    def test_the_candidate_image_is_not_compared(self):
        own = (0x80800000, 0x80800400)
        got = c.compare_case(case(), case(net=[[0x80A00010, 7], [0x80800100, 9]]), PROTO, {"g": CALLEE}, STACK, own)
        self.assertEqual(got["verdict"], c.SAME)

    def test_call_order_and_arguments(self):
        other = case(events=[call("f"), call("h"), call("g", 0, 1, 0x3F800000)])
        got = verdict(case(), other)
        self.assertEqual(
            (got["verdict"], got["difference"]["expected"], got["difference"]["got"]), (c.DIFFERS, "g", "h")
        )
        missing = verdict(case(), case(events=[call("f")]))
        self.assertEqual((missing["difference"]["expected"], missing["difference"]["got"]), ("g", "return"))
        wrong = verdict(case(), case(events=[call("f"), call("g", 0, 2, 0x3F800000)]))
        self.assertEqual((wrong["difference"]["kind"], wrong["difference"]["argument"]), ("call-args", "x(a1)"))
        float_arg = verdict(case(), case(events=[call("f"), call("g", 0, 1, 0x40000000)]))
        self.assertEqual(float_arg["difference"]["argument"], "y(f12)")
        self.assertEqual(float_arg["verdict"], c.DIFFERS)

    def test_the_address_of_a_local_passed_to_a_callee_is_not_compared(self):
        local = lambda a: case(events=[call("f"), call("g", 0, a, 0x3F800000)])  # noqa: E731
        self.assertEqual(verdict(local(0x803F1F70), local(0x803F1F78))["verdict"], c.SAME)
        self.assertEqual(verdict(local(0x803F1F70), local(0x80A00010))["verdict"], c.DIFFERS)  # one left the frame

    def test_a_register_the_callee_does_not_take_does_not_matter(self):
        self.assertEqual(verdict(case(), case(events=[call("f"), call("g", 99, 1, 0x3F800000)]))["verdict"], c.SAME)

    def test_a_callee_without_a_prototype_is_a_note_not_a_verdict(self):
        got = verdict(case(), case(events=[call("f"), call("g", 0, 9, 0x3F800000)]), callees={})
        self.assertEqual(got["verdict"], c.SAME)
        self.assertTrue(any("no prototype" in n for n in got["notes"]))

    def test_clobbered_callee_saved_registers(self):
        got = verdict(case(), case(saved=["r17"]))
        self.assertEqual((got["verdict"], got["difference"]["registers"]), (c.DIFFERS, ["r17"]))

    def test_status_rules(self):
        untestable = verdict(case(status="mmio", detail="mmio A4300000 4"), case())
        self.assertEqual(untestable["verdict"], c.UNTESTABLE)
        self.assertIn("original mmio", untestable["reason"])
        broke = verdict(case(), case(status="timeout", detail="no return"))
        self.assertEqual((broke["verdict"], broke["difference"]["kind"]), (c.DIFFERS, "status"))

    def test_unseeded_reads_are_notes_only(self):
        got = verdict(case(unseeded=[(0x80100000, 4)]), case(unseeded=[(0x80100000, 4), (0x80100004, 4)]))
        self.assertEqual(got["verdict"], c.SAME)
        self.assertIn("candidate reads 1 unseeded", got["notes"][0])


class OverallTests(unittest.TestCase):
    def rows(self, *verdicts):
        out = []
        for index, v in enumerate(verdicts):
            row = {
                "name": f"c{index}",
                "verdict": v,
                "reason": "original unmapped: x" if v == c.UNTESTABLE else "boom",
                "difference": {"kind": "return"} if v == c.DIFFERS else None,
                "notes": [],
            }
            out.append(row)
        return out

    def test_classes(self):
        labels = [{"a": str(i)} for i in range(3)]
        same = c.overall(self.rows(c.SAME, c.UNTESTABLE, c.SAME), labels)
        self.assertEqual((same["verdict"], same["counts"]), (c.SAME, {"same": 2, "differs": 0, "untestable": 1}))
        differs = c.overall(self.rows(c.SAME, c.DIFFERS, c.DIFFERS), labels)
        self.assertEqual((differs["verdict"], differs["first_difference"]["case"]), (c.DIFFERS, "c1"))
        self.assertIn("c1 (a=1): boom", differs["reason"])
        none = c.overall(self.rows(c.UNTESTABLE, c.UNTESTABLE, c.UNTESTABLE), labels)
        self.assertEqual(none["verdict"], c.UNTESTABLE)
        self.assertEqual(none["untestable_reasons"], {"original unmapped": 3})


class ReasonTests(unittest.TestCase):
    def test_untestable_reasons_group_by_kind_not_by_address(self):
        self.assertEqual(c.reason_key("original fatal: pause-self 00000000 0 last 8014"), "original fatal pause-self")
        self.assertEqual(c.reason_key("original unmapped: unmapped 00000004 4 last 80A0"), "original unmapped")
        self.assertEqual(c.reason_key(None), "unknown")


class Immutable(unittest.TestCase):
    def test_comparison_does_not_change_its_inputs(self):
        a, b = case(), case(ret=9)
        before = copy.deepcopy((a, b))
        verdict(a, b)
        self.assertEqual((a, b), before)


if __name__ == "__main__":
    unittest.main()
