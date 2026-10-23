"""C types of the game's variables laid out for the port and the original (module statetypes)."""

import unittest

from abu_decomp import statetypes as st

PRELUDE = """
typedef unsigned char u8; typedef signed short s16; typedef unsigned short u16; typedef unsigned int u32;
typedef float f32; typedef double f64;
typedef struct Pair { u8 a; u16 b; u32 c; } Pair;
struct Mixed { u8 tag; f64 value; s16 small; };
struct Bits { u32 on : 1; u32 mode : 3; u32 rest : 28; u8 after; };
union Either { u32 word; u8 bytes[4]; };
struct Holder { struct Pair pair; u8 pad[3]; struct Mixed* next; union Either either; u32 (*callback)(int); };
extern struct Holder gHolder;
extern u32 gTable[];
extern u8 gNames[4][8];
extern Pair gPairs[3];
extern const u32 gConstant;
u32 gDefined = 0x1234;
u32 gArray[2] = { 1, 2 };
"""


def database() -> st.TypeDb:
    db = st.TypeDb()
    for statement in st.statements(PRELUDE):
        db.add(statement)
    return db


class SplitTests(unittest.TestCase):
    def test_function_bodies_and_initialisers_are_cut(self):
        text = "int f(int a) { return a + 1; }\nu32 x[2] = { 1, 2 };\nstruct S { int a; } s = {3};\nextern u8 y;\n"
        self.assertEqual(st.statements(text), ["u32 x[2]", "struct S { int a; } s", "extern u8 y"])

    def test_strings_and_equals_in_expressions_do_not_confuse_it(self):
        text = 'char *p = "a;{=";\nenum { A = 1, B = A == 1 };\nint v[3];\n'
        self.assertEqual(st.statements(text), ["char *p", "enum { A = 1, B = A == 1 }", "int v[3]"])

    def test_gcc_extensions_are_stripped(self):
        self.assertEqual(st.clean('extern int x __attribute__((aligned(8), unused)) __asm__("y")'), "extern int x")
        self.assertEqual(st.clean("__extension__ static __inline__ int k"), "static   int k")

    def test_chunks_skip_text_already_read(self):
        text = '# 1 "a.h" 1\nint a;\n# 5 "b.c"\nint b;\n'
        seen: set = set()
        first = st.chunks(text, seen)
        self.assertEqual([name for name, _ in first], ["a.h", "b.c"])
        self.assertEqual(st.chunks(text, seen), [])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.db = database()

    def layout(self, name: str, abi: str) -> st.Layout:
        return st.Layouts(self.db, abi).of(self.db.variables[name].type)

    def test_scalars_arrays_and_padding_follow_each_abi(self):
        pair = self.layout("gPairs", "i386")
        self.assertEqual(pair.size, 24)
        self.assertEqual(pair.runs[:3], [(0, "b", 1), (2, "h", 1), (4, "w", 1)])
        self.assertEqual(self.layout("gNames", "mips").runs, [(0, "b", 32)])

    def test_double_alignment_differs_between_abis(self):
        mixed = {abi: st.Layouts(self.db, abi).of(self.db.tags["struct Mixed"]) for abi in st.ABIS}
        self.assertEqual((mixed["i386"].size, mixed["mips"].size), (16, 24))
        self.assertEqual([r[1] for r in mixed["i386"].runs], [r[1] for r in mixed["mips"].runs])
        self.assertEqual(mixed["i386"].runs[1][0], 4)
        self.assertEqual(mixed["mips"].runs[1][0], 8)

    def test_bitfields_and_unions_have_no_host_independent_elements(self):
        bits = self.layout("gHolder", "i386")
        bit_struct = st.Layouts(self.db, "i386").of(self.db.tags["struct Bits"])
        self.assertEqual(bit_struct.runs, [(4, "b", 1)])  # the bitfield word is left out, the byte after it kept
        self.assertEqual(st.Layouts(self.db, "mips").of(self.db.tags["union Either"]).runs, [])
        kinds = [k for _o, k, _n in bits.runs]
        self.assertIn("p", kinds)  # the callback and next pointers

    def test_open_array_takes_its_count_from_the_caller(self):
        decl = self.db.variables["gTable"]
        with self.assertRaises(st.Unsized):
            st.Layouts(self.db, "i386").of(decl.type)
        self.assertEqual(st.Layouts(self.db, "i386").open_array(decl.type, 5).runs, [(0, "w", 5)])

    def test_const_data_is_not_state(self):
        self.assertTrue(st.is_const(self.db.variables["gConstant"].type))
        self.assertFalse(st.is_const(self.db.variables["gDefined"].type))

    def test_definitions_register_without_their_initialiser(self):
        self.assertEqual(self.layout("gArray", "i386").runs, [(0, "w", 2)])

    def test_shape_names_the_element_that_differs(self):
        shape = self.layout("gHolder", "i386").shape
        self.assertEqual(st.elements(shape), sum(n for _o, _k, n in self.layout("gHolder", "mips").runs))
        self.assertEqual(st.field_at(shape, 0), ("pair.a", "b"))
        self.assertEqual(st.field_at(shape, 2), ("pair.c", "w"))
        pairs = self.layout("gPairs", "i386").shape
        self.assertEqual(st.field_at(pairs, 4), ("[1].b", "h"))

    def test_unknown_type_is_refused_not_guessed(self):
        db = st.TypeDb()
        db.add("extern Missing gGone")
        self.assertEqual(db.failed, 1)
        self.assertNotIn("gGone", db.variables)
        db.add("extern struct Opaque gOpaque")
        with self.assertRaises(st.Unsized):
            st.Layouts(db, "i386").of(db.variables["gOpaque"].type)


if __name__ == "__main__":
    unittest.main()
