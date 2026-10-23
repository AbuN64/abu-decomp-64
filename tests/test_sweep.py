import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from abu_decomp import DecompError, cli, sweep

_DIRS: list[tempfile.TemporaryDirectory] = []


def tearDownModule():
    for d in _DIRS:
        d.cleanup()


def rules(text: str) -> list[sweep.Rule]:
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "rules.toml"
        file.write_text(text)
        return sweep.load_rules([file])


class Tree:
    def __init__(self, files: dict[str, str]) -> None:
        self.dir = tempfile.TemporaryDirectory()
        _DIRS.append(self.dir)
        self.root = Path(self.dir.name)
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)

    def sweep(self, rule_text: str, **options):
        return sweep.run(rules(rule_text), {"t": self.root}, **options)


PATTERN = """
[[rule]]
id = "wide"
kind = "pattern"
why = "w"
paths = ["**/*.c"]
match = '\\*\\s*\\(\\s*u32\\s*\\*\\s*\\)'
unless = 'swap'
"""

DECL_USE = """
[[rule]]
id = "bytes"
kind = "decl-use"
why = "w"
paths = ["**/*.c", "**/*.h"]
declare = '^(?P<local>static\\s+)?(?:extern\\s+)?(?:u32|int)\\s+(?P<datum>\\w+)\\s*\\['
use = '\\(\\s*u8\\s*\\*\\s*\\)\\s*&?\\s*(?P<datum>\\w+)\\b(?!\\s*(?:->|\\.|\\[))'
"""

ARITY = """
[[rule]]
id = "args"
kind = "call-arity"
why = "w"
paths = ["**/*.c"]
"""


class PatternTests(unittest.TestCase):
    def test_hits_a_line_and_skips_unless_and_comments(self):
        tree = Tree(
            {
                "a.c": "int f(void) {\n  x = *(u32 *)p;\n  y = *(u32 *)q; /* swap */\n"
                "  /* *(u32 *)z */\n  w = *(u32 *)swap_it(p);\n}\n"
            }
        )
        result = tree.sweep(PATTERN)
        self.assertEqual([(h["file"], h["line"], h["in_fn"]) for h in result["hits"]], [("a.c", 2, "f")])
        self.assertEqual(result["rules"][0]["hits"], 1)

    def test_multiline_hit_is_the_line_the_match_starts_on(self):
        text = """
[[rule]]
id = "table"
kind = "pattern"
why = "w"
paths = ["**/*.c"]
multiline = true
match = 'u32\\s+\\w+\\[\\]\\s*=\\s*\\{[^}]*0x41424344'
"""
        tree = Tree({"d.c": "int a;\n\nu32 t[] = {\n  1,\n  0x41424344,\n};\n"})
        self.assertEqual([h["line"] for h in tree.sweep(text)["hits"]], [3])

    def test_a_named_datum_group_is_kept(self):
        text = PATTERN.replace("match = '", "match = '(?P<datum>D_\\w+).*").replace(
            "unless = 'swap'", "unless = 'nothing'"
        )
        tree = Tree({"a.c": "void f(void) { D_1 = *(u32 *)p; }\n"})
        self.assertEqual(tree.sweep(text)["hits"][0].get("datum"), "D_1")

    def test_globs_select_files_and_skip(self):
        tree = Tree({"a.c": "x = *(u32 *)p;\n", "tests/b.c": "x = *(u32 *)p;\n", "c.h": "x = *(u32 *)p;\n"})
        text = PATTERN.replace('paths = ["**/*.c"]', 'paths = ["**/*.c"]\nskip = ["tests/**"]')
        self.assertEqual([h["file"] for h in tree.sweep(text)["hits"]], ["a.c"])


class DeclUseTests(unittest.TestCase):
    def test_a_word_array_read_through_a_byte_cast_is_a_hit(self):
        tree = Tree(
            {
                "d.h": "extern u32 table[];\nextern int *ptr;\n",
                "a.c": "void f(void) {\n  b = (u8 *)table;\n  c = (u8 *)ptr;\n  d = (u8 *)table->x;\n"
                "  e = (u8 *)table[1];\n}\n",
            }
        )
        result = tree.sweep(DECL_USE)
        self.assertEqual([(h["line"], h["datum"]) for h in result["hits"]], [(2, "table")])
        self.assertEqual(result["hits"][0]["declared"], "d.h:1")

    def test_a_static_array_is_its_own_files_only(self):
        tree = Tree(
            {
                "a.c": "static u32 own[4];\nvoid f(void) { x = (u8 *)own; }\n",
                "b.c": "void g(u8 *own) { y = (u8 *)own; }\n",
            }
        )
        self.assertEqual([h["file"] for h in tree.sweep(DECL_USE)["hits"]], ["a.c"])


class ArityTests(unittest.TestCase):
    def hits(self, files: dict[str, str]):
        return Tree(files).sweep(ARITY)["hits"]

    def test_a_call_with_fewer_arguments_names_callee_and_caller(self):
        hits = self.hits(
            {
                "def.c": "float walk(Stream *p, float t) {\n  return t;\n}\n",
                "use.c": "extern void walk();\nvoid wrap(void) {\n  walk();\n  walk(a, b);\n  walk(f(1, 2));\n}\n",
            }
        )
        self.assertEqual(
            [(h["line"], h["fn"], h["callers"]) for h in hits], [(3, "walk", ["wrap"]), (5, "walk", ["wrap"])]
        )
        self.assertIn("takes 2 parameters", hits[0]["detail"])

    def test_declarations_members_and_variadics_are_not_calls(self):
        hits = self.hits(
            {
                "a.c": "int one(int a) {\n return a;\n}\nint many(int a, ...) {\n return a;\n}\n"
                "void g(void) {\n  s->one();\n  many();\n  int one(int a);\n  static int one();\n}\n"
            }
        )
        self.assertEqual(hits, [])

    def test_functions_defined_with_different_counts_are_skipped(self):
        hits = self.hits(
            {"a.c": "void f(int a) {\n}\nvoid g(void) {\n f();\n}\n", "b.c": "void f(int a, int b) {\n}\n"}
        )
        self.assertEqual(hits, [])

    def test_mode_any_also_flags_extra_arguments(self):
        text = ARITY + 'mode = "any"\n'
        hits = Tree({"a.c": "void f(int a) {\n}\nvoid g(void) {\n f(1, 2);\n}\n"}).sweep(text)["hits"]
        self.assertEqual(len(hits), 1)


class BaselineTests(unittest.TestCase):
    def test_a_baseline_marks_known_hits_and_survives_line_moves(self):
        tree = Tree({"a.c": "void f(void) {\n  x = *(u32 *)p;\n}\n"})
        base = tree.root / "base.tsv"
        first = tree.sweep(PATTERN)
        self.assertEqual(sweep.write_baseline(base, first["hits"]), 1)
        (tree.root / "a.c").write_text("/* moved */\n\nvoid f(void) {\n  x = *(u32 *)p;\n  y = *(u32 *)q;\n}\n")
        again = tree.sweep(PATTERN, baseline=base)
        self.assertEqual([(h["line"], h["new"]) for h in again["hits"]], [(4, False), (5, True)])
        self.assertEqual(again["rules"][0]["new"], 1)

    def test_identical_lines_are_numbered(self):
        hits = Tree({"a.c": "x = *(u32 *)p;\nx = *(u32 *)p;\n"}).sweep(PATTERN)["hits"]
        self.assertEqual(len(set(sweep.fingerprints(hits))), 2)


class RuleErrorTests(unittest.TestCase):
    def test_bad_rules_name_the_rule(self):
        for text, word in (
            (PATTERN.replace('kind = "pattern"', 'kind = "magic"'), "kind"),
            (PATTERN + "colour = 1\n", "unknown rule keys"),
            (DECL_USE.replace("(?P<datum>\\w+)\\s*\\['", "\\w+\\s*\\['"), "datum"),
            (PATTERN.replace("match = '", "match = '("), "rule wide"),
            (PATTERN + PATTERN, "twice"),
        ):
            with self.assertRaises(DecompError, msg=word) as raised:
                rules(text)
            self.assertIn(word, str(raised.exception))

    def test_a_rule_names_only_trees_that_exist(self):
        with self.assertRaises(DecompError):
            Tree({}).sweep(PATTERN.replace('why = "w"', 'why = "w"\ntrees = ["ghost"]'))

    def test_only_unknown_rule(self):
        with self.assertRaises(DecompError):
            Tree({}).sweep(PATTERN, only=["nope"])


class FindingsAndCliTests(unittest.TestCase):
    def test_hits_become_findings_that_join_a_crash_bucket(self):
        tree = Tree({"def.c": "void f(int a) {\n}\n", "use.c": "void g(void) {\n f();\n}\n"})
        result = tree.sweep(ARITY)
        row = sweep.to_findings(result, {"t": tree.root})[0]
        self.assertEqual(
            (row["kind"], row["fn"], row["callers"], row["signature"]), ("sweep", "f", ["g"], "sweep args (t)")
        )
        self.assertTrue(row["evidence"].endswith("use.c:2"))

    def test_identical_lines_stay_distinct_findings(self):
        tree = Tree({"a.c": "x = *(u32 *)p;\nx = *(u32 *)p;\n"})
        rows = sweep.to_findings(tree.sweep(PATTERN), {"t": tree.root})
        self.assertEqual(len({r["fp"] for r in rows}), 2)

    def test_cli_exit_status_follows_new_block_hits_and_updates_the_baseline(self):
        tree = Tree(
            {"a.c": "x = *(u32 *)p;\n", "r.toml": PATTERN.replace('why = "w"', 'why = "w"\nseverity = "block"')}
        )
        base = tree.root / "b.tsv"
        args = ["sweep", "--rules", str(tree.root / "r.toml"), "--tree", f"t={tree.root}", "--baseline", str(base)]

        def call(extra):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(args + extra)
            return code, json.loads(out.getvalue()), err.getvalue()

        code, envelope, text = call([])
        self.assertEqual(code, 1)
        self.assertIn("wide [pattern, block]: 1 hits, 1 new", text)
        self.assertEqual(envelope["findings"][0]["key"], "sweep.wide")
        code, envelope, _ = call(["--update-baseline"])
        self.assertEqual((code, envelope["result"]["baseline_written"]), (1, 1))  # judged before it was written
        code, envelope, _ = call([])
        self.assertEqual((code, envelope["result"]["rules"][0]["new"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
