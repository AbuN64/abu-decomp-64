import unittest

from abu_decomp.behave import prototype as p

HEADER = """
typedef signed int s32;
typedef struct Vec { float x, y, z; } Vec;
typedef struct Actor Actor;
typedef Actor *ActorRef;
typedef void (*Hook)(s32);
extern s32 func_8024E62C_de(Actor *);
extern void func_80271F9C_de(Vec *, Vec *, f32);
void func_80245D30_de(Actor *arg0, ActorRef arg1, Vec v, s32 arg3) {
    x = func_8024E62C_de(arg0);
    func_80271F9C_de(&a, &b, 1.0f);
}
extern s32 printf_like(void *, const char *, ...);
extern void nothing(void);
extern f64 wide(s64 big, u8 small, void (*cb)(s32 a, s32 b));
"""


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        self.aliases = p.scan_aliases([HEADER])

    def parse(self, name):
        return p.parse_declaration(HEADER, name, self.aliases, "h.h")

    def test_aliases_follow_typedefs(self):
        self.assertEqual(self.aliases["Vec"], "struct")
        self.assertEqual(self.aliases["ActorRef"], "pointer")
        self.assertEqual(self.aliases["Hook"], "funcptr")

    def test_a_declaration_and_a_definition_give_the_same_kind_of_row(self):
        decl = self.parse("func_8024E62C_de")
        self.assertEqual((decl.ret, [x.kind for x in decl.params], decl.known), ("int32", ["pointer"], True))
        self.assertTrue(decl.source.startswith("h.h:"))
        define = self.parse("func_80245D30_de")
        self.assertEqual([x.kind for x in define.params], ["pointer", "pointer", "struct", "int32"])
        self.assertEqual([x.name for x in define.params], ["arg0", "arg1", "v", "arg3"])
        self.assertTrue(define.blind)  # a by-value struct is a guess

    def test_calls_are_not_declarations(self):
        self.assertIsNone(p.parse_declaration("void f(void) { y = g(1); return g(2); }", "g", {}))

    def test_void_variadic_and_function_pointer_parameters(self):
        self.assertEqual(self.parse("nothing").params, ())
        variadic = self.parse("printf_like")
        self.assertTrue(variadic.variadic and variadic.blind and [x.kind for x in variadic.params] == ["pointer"] * 2)
        wide = self.parse("wide")
        self.assertEqual((wide.ret, [x.kind for x in wide.params]), ("double", ["int64", "uint8", "funcptr"]))

    def test_inferred_prototype_is_four_unknown_words(self):
        guess = p.inferred("mystery")
        self.assertEqual(([x.kind for x in guess.params], guess.known, guess.blind), (["word"] * 4, False, True))

    def test_classify(self):
        cases = {
            "const char *": "pointer",
            "u8": "uint8",
            "unsigned int": "uint32",
            "long long": "int64",
            "f32": "float",
            "struct Foo": "struct",
            "Foo": "word",
            "s32 (*)(void)": "funcptr",
            "void": "void",
        }
        for text, kind in cases.items():
            self.assertEqual(p.classify(text), kind, text)


class SlotTests(unittest.TestCase):
    def slots(self, text, name="f"):
        return [(s.param.name, s.where) for s in p.argument_slots(p.parse_declaration(text, name, {}))]

    def test_integers_fill_a0_to_a3_then_the_stack(self):
        # o32: the 5th word is read at sp+16 on entry (func_80442D10_de loads its 5th parameter from 0x10 off sp)
        got = self.slots("void f(s32 a, s32 b, s32 c, s32 d, s32 e, s32 g);")
        self.assertEqual([w for _, w in got], [("a0",), ("a1",), ("a2",), ("a3",), ("stack:16",), ("stack:20",)])

    def test_leading_floats_use_f12_and_f14_but_still_use_up_argument_words(self):
        got = self.slots("void f(f32 x, f32 y, s32 n, f32 z);")
        self.assertEqual(got, [("x", ("f12",)), ("y", ("f14",)), ("n", ("a2",)), ("z", ("a3",))])

    def test_a_float_after_an_integer_travels_in_an_integer_register(self):
        self.assertEqual(self.slots("void f(s32 n, f32 x);"), [("n", ("a0",)), ("x", ("a1",))])

    def test_64_bit_values_take_an_aligned_pair(self):
        got = self.slots("void f(s32 a, s64 b, s32 c);")
        self.assertEqual(got, [("a", ("a0",)), ("b", ("a2", "a3")), ("c", ("stack:16",))])


if __name__ == "__main__":
    unittest.main()
