import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from abu_decomp import DecompError
from abu_decomp.proposals import check_steps, expand, line, run, source_files, summarize

STRUCTURE = {"name": "structure", "args": ["structure", "--json", "--paths", "{files}"]}
NAMES = {"name": "names", "args": ["name", "next", "--json"]}


class Recipe:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, tool, profile, args, **kwargs):
        self.calls.append((tool, tuple(args), kwargs))
        result = self.results[args[0]]
        if isinstance(result, DecompError):
            raise result
        ok = result is not None
        document = {"ok": ok, **(result or {}), "findings": [] if ok else [{"message": "map is stale"}]}
        return SimpleNamespace(ok=ok, returncode=0 if ok else 1, document=document)


class StepTests(unittest.TestCase):
    def test_only_proposal_verbs_are_accepted(self):
        self.assertEqual(check_steps(Path("s.json"), "k", [STRUCTURE, NAMES]), [STRUCTURE, NAMES])
        for args in (["apply"], ["name", "submit", "x"], ["name"], ["tui"]):
            with self.assertRaises(DecompError) as raised:
                check_steps(Path("s.json"), "k", [{"name": "x", "args": args}])
            self.assertIn("s.json:k[0]", str(raised.exception))
        for bad in ([], "x", [{"name": "x"}], [{"name": "x", "args": [], "extra": 1}]):
            with self.assertRaises(DecompError):
                check_steps(Path("s.json"), "k", bad)

    def test_files_expand_to_one_argument_each_and_none_skips_the_step(self):
        self.assertEqual(expand(STRUCTURE["args"], ["a.c", "b.h"]), ["structure", "--json", "--paths", "a.c", "b.h"])
        self.assertIsNone(expand(STRUCTURE["args"], []))
        self.assertEqual(expand(NAMES["args"], []), ["name", "next", "--json"])

    def test_source_files_keep_existing_sources_only(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for file in ("a.c", "b.H", "c.s", "d.c"):
                (root / file).write_text("x")
            self.assertEqual(source_files(root, ["b.H", "a.c", "c.s", "gone.c", "a.c"]), ["a.c", "b.H"])

    def test_summary_keeps_numbers_short_strings_and_list_sizes(self):
        result = {
            "moves": 2,
            "files": ["a", "b"],
            "patch": "x" * 500,
            "path": ".unbake/p.diff",
            "suggestions": [{"m": 1}, {"m": 2}],
        }
        self.assertEqual(
            summarize(result),
            {"moves": 2, "files": 2, "path": ".unbake/p.diff", "suggestions": 2, "head": ['{"m": 1}', '{"m": 2}']},
        )


class RunTests(unittest.TestCase):
    def test_each_step_runs_on_the_landed_files_and_a_failure_does_not_stop_the_rest(self):
        recipe = Recipe({"structure": None, "name": {"moves": 0, "targets": 12}})
        found = run(None, Path("/t"), [STRUCTURE, NAMES], ["a.c"], invoke=recipe)
        self.assertEqual(found["structure"], {"ok": False, "error": "map is stale"})
        self.assertEqual(found["names"], {"ok": True, "moves": 0, "targets": 12})
        self.assertEqual(
            recipe.calls[0], ("recipe", ("structure", "--json", "--paths", "a.c"), {"project": Path("/t")})
        )
        self.assertEqual(line(found), "recipe proposes structure failed: map is stale; names 0 moves, 12 targets")

    def test_a_tool_refusal_and_missing_files_are_reported_not_raised(self):
        recipe = Recipe({"structure": DecompError("dependency.timeout", "recipe exceeded 5s")})
        found = run(None, None, [STRUCTURE], ["a.c"], invoke=recipe)
        self.assertEqual(found["structure"], {"ok": False, "error": "recipe exceeded 5s"})
        self.assertEqual(recipe.calls[0][2], {})
        skipped = run(None, None, [STRUCTURE], [], invoke=recipe)
        self.assertEqual(skipped["structure"]["skipped"], "no landed source files")
        self.assertEqual(len(recipe.calls), 1)
        json.dumps(found)
