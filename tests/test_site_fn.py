"""The function screen's record (module site_fn) and the pure logic of static/site/fn.js."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import site_fn

FN_JS = Path(site_fn.__file__).parent / "static" / "site" / "fn.js"

CALL = {
    "verdict": "differs",
    "compared": 7,
    "unmatched_oracle": 1,
    "unmatched_port": 0,
    "cause": "own",
    "first": {
        "what": "result",
        "caller": "Caller_X",
        "ordinal": 3,
        "frame": {"oracle": 40, "port": 41},
        "diffs": [{"field": "return", "kind": "int", "oracle": "0x2", "port": "0x1"}],
        "oracle": {"args": ["0x5"], "ret": "0x2", "writes": None},
        "port": {"args": ["0x5"], "ret": "0x1", "writes": {"buf": "00"}},
    },
}
BEHAVE = [
    {
        "verdict": "same",
        "count": 2,
        "arguments": [{"where": "a0", "value": "3", "name": "n", "kind": "int32"}],
        "outputs": {
            "oracle": {"status": "ok", "return": "v0=3"},
            "port": {"status": "ok", "return": "v0=3"},
        },
    },
    {
        "verdict": "differs",
        "count": 1,
        "arguments": [{"where": "a0", "value": "4", "name": "n", "kind": "int32"}],
        "outputs": {
            "oracle": {"status": "ok", "return": "v0=4"},
            "port": {"status": "crashed", "return": None},
        },
    },
    {"verdict": "untestable", "arguments": [], "outputs": {"oracle": {"status": "ok", "return": "void"}, "port": None}},
]
BAKED = {
    "name": "Fn_A",
    "file": "src/a.c",
    "first": 10,
    "size": 64,
    "state": "exact",
    "reach": 3,
    "behaviour": "differs",
    "html": ['<span class="k">int</span> x;'],
    "lines": [[5, 1, 2]],
    "callers": ["Caller_X"],
    "callees": [],
    "movies": ["menu", "intro"],
    "call": CALL,
    "behave": BEHAVE,
}


RUN_FIELDS = {"lane": "l", "movie": "m", "status": "done", "poll": 5, "polls": 9, "outcome": "ok"}


def run_row(rid, movie, started, kind="tas", target=None):
    return {"id": rid, "movie": movie, "started": started, "kind": kind, "target": target, "status": "done"}


class Server:
    """What site_fn reads of tas serve: the baked site folder, the config, and the run row builder."""

    def __init__(self, root: Path, rows):
        self.site = root / "site"
        (self.site / "fn").mkdir(parents=True)
        (self.site / "fn" / "Fn_A.json").write_text(json.dumps(BAKED))
        self.cfg = {"runs": root}
        self.rows = [(root / "plays" / row["id"], row) for row in rows]

    def row(self, path, row):
        return row


class CaseTest(unittest.TestCase):
    def test_verdict_case_carries_both_sides_and_the_first_divergence(self):
        case = site_fn.verdict_case("Fn_A", CALL)
        self.assertEqual(
            (case["caller"], case["ordinal"], case["frame"]), ("Caller_X", 3, {"port": 41, "original": 40})
        )
        self.assertEqual(case["inputs"], {"port": ["0x5"], "original": ["0x5"]})
        self.assertEqual(case["outputs"]["port"]["return"], "0x1")
        self.assertEqual(case["outputs"]["original"]["return"], "0x2")
        self.assertEqual(case["outputs"]["port"]["writes"], '{"buf":"00"}')
        self.assertEqual(case["divergence"], [{"field": "return", "kind": "int", "port": "0x1", "original": "0x2"}])
        self.assertEqual(
            (case["verdict"], case["compared"], case["unmatched"]), ("differs", 7, {"port": 0, "original": 1})
        )

    def test_verdict_without_a_diverging_call_makes_no_case(self):
        self.assertIsNone(site_fn.verdict_case("Fn_A", {"verdict": "same", "compared": 3, "first": None}))

    def test_behave_case_diverges_only_where_status_or_return_differ(self):
        same, status, blind = (site_fn.behave_case("Fn_A", i, row) for i, row in enumerate(BEHAVE))
        self.assertEqual((same["divergence"], same["compared"], same["inputs"]["port"]), ([], 2, ["a0=3"]))
        self.assertEqual(
            status["divergence"],
            [
                {"field": "status", "kind": "status", "port": "crashed", "original": "ok"},
                {"field": "return", "kind": "return", "port": None, "original": "v0=4"},
            ],
        )
        self.assertIsNone(blind["outputs"]["port"])
        self.assertEqual(blind["divergence"], [])

    def test_cases_start_with_the_verdict_case_then_the_behave_rows_in_order(self):
        cases = site_fn.cases_of(BAKED)
        self.assertEqual([c["ordinal"] for c in cases], [3, 0, 1, 2])
        self.assertEqual(site_fn.cases_of({"name": "Fn_B"}), [])

    def test_every_case_has_exactly_the_contract_fields(self):
        keys = {"function", "ordinal", "caller", "frame", "inputs", "outputs", "divergence", "verdict"}
        keys |= {"compared", "unmatched"}
        for case in site_fn.cases_of(BAKED):
            self.assertEqual(set(case), keys)


class AnswerTest(unittest.TestCase):
    def serve(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Server(Path(tmp.name), rows)

    def answer(self, server, rest="Fn_A", method="GET", oracle=None):
        with (
            mock.patch.object(site_fn.tas, "run_rows", return_value=server.rows),
            mock.patch.object(site_fn.tas_coverage, "read_oracle", side_effect=lambda run: oracle(run)),
        ):
            return site_fn.answer(server, method, rest, "", b"")

    def test_runs_are_the_newest_per_reaching_movie_plus_runs_targeting_the_function(self):
        rows = [
            run_row("old", "menu", 1.0),
            run_row("new", "menu", 5.0),
            run_row("other", "credits", 9.0),
            run_row("u1", "", 3.0, kind="unit", target="Fn_A"),
            run_row("u2", "", 4.0, kind="unit", target="Fn_B"),
        ]
        server = self.serve(rows)
        (server.cfg["runs"] / "plays" / "new" / "stream").mkdir(parents=True)
        (server.cfg["runs"] / "plays" / "new" / "stream" / "index.json").write_text("{}")
        status, got = self.answer(server, oracle=lambda run: ({"Fn_A": (12, 4)}, 100) if run.name == "new" else None)
        self.assertEqual(status, 200)
        self.assertEqual(
            [(r["id"], r["kind"], r["stream"], r["first"]) for r in got["runs"]],
            [("u1", "unit", False, None), ("new", "tas", True, 12)],
        )
        self.assertEqual(got["name"], "Fn_A")
        self.assertEqual(len(got["cases"]), 4)

    def test_a_run_whose_oracle_never_entered_the_function_has_no_first_poll(self):
        server = self.serve([run_row("only", "menu", 1.0)])
        _, got = self.answer(server, oracle=lambda run: ({}, 10))
        self.assertEqual([(r["id"], r["first"]) for r in got["runs"]], [("only", None)])

    def test_the_bakes_first_sighting_names_the_poll_when_the_run_left_no_trace_and_ran_that_movie(self):
        server = self.serve([run_row("m", "menu", 1.0), run_row("i", "intro", 2.0)])
        (server.site / "fn" / "Fn_A.json").write_text(json.dumps({**BAKED, "oracle": {"movie": "menu", "poll": 9}}))
        _, got = self.answer(server, oracle=lambda run: None)
        self.assertEqual({r["id"]: r["first"] for r in got["runs"]}, {"m": 9, "i": None})

    def test_unknown_function_and_bad_paths_refuse(self):
        server = self.serve([])
        self.assertEqual(self.answer(server, "Nope")[0], 404)
        self.assertEqual(self.answer(server, "")[0], 400)
        self.assertEqual(self.answer(server, "a/b")[0], 400)
        self.assertEqual(self.answer(server, "../x")[0], 400)
        self.assertEqual(self.answer(server, "Fn_A", method="POST")[0], 405)

    def test_no_game_names_in_the_region(self):
        banned = ("zelda", "mario", "ragewars", "banjo", "ocarina")
        for path in (Path(site_fn.__file__), FN_JS):
            self.assertFalse([w for w in banned if w in path.read_text().lower()], path.name)


@unittest.skipUnless(shutil.which("node"), "node runs the page module's pure logic")
class PageLogicTest(unittest.TestCase):
    def js(self, expr: str):
        script = f'import * as fn from "{FN_JS.as_uri()}"; process.stdout.write(JSON.stringify({expr}));'
        argv = ["node", "--input-type=module", "-e", script]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_run_link_uses_the_first_poll_and_encodes_the_run(self):
        self.assertEqual(self.js('fn.runHref({id: "a b", first: 12})'), "#/stream/a%20b?poll=12")
        self.assertEqual(self.js('fn.runHref({id: "r1", first: 0})'), "#/stream/r1?poll=0")
        self.assertEqual(self.js('fn.runHref({id: "r1", first: null})'), "#/stream/r1")

    def test_runs_group_by_contract_kind_order_and_skip_empty_kinds(self):
        runs = '[{id:"t",kind:"tas"},{id:"u",kind:"unit"},{id:"t2",kind:"tas"}]'
        got = self.js(f'fn.groupRuns({runs}, ["unit","set","tas"])')
        self.assertEqual(
            [(g["kind"], [r["id"] for r in g["runs"]]) for g in got], [("unit", ["u"]), ("tas", ["t", "t2"])]
        )

    def test_gutter_entries_show_counts_branches_and_hit_state(self):
        self.assertEqual(self.js("fn.countCell(null)"), {"text": "", "state": ""})
        self.assertEqual(self.js("fn.countCell([0])"), {"text": "0", "state": "miss"})
        self.assertEqual(self.js("fn.countCell([1500, 1, 2])"), {"text": "1,500 1/2", "state": "hit"})

    def test_preview_frames_come_from_the_stream_files_of_the_run(self):
        self.assertEqual(self.js('fn.previewSrc({id: "r1", first: 7}, "original")'), "/stream/r1/original/7.jpg")

    def test_an_event_concerns_the_screen_by_target_run_or_movie(self):
        rec = '{name:"F",movies:["menu"],runs:[{id:"r1"}]}'
        for fields, expected in (
            ('{target:"F"}', True),
            ('{run:"r1",movie:"x"}', True),
            ('{run:"r9",movie:"menu"}', True),
            ('{run:"r9",movie:"x",target:"G"}', False),
        ):
            self.assertIs(self.js(f"fn.concerns({{fields: {fields}}}, {rec})"), expected, fields)

    def test_mount_renders_code_runs_by_kind_and_case_rows_then_refetches_on_a_concerning_event(self):
        fake = """
        class Node {
          constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.isConnected = true; }
          setAttribute(k, v) { this.attrs[k] = v; }
          append(...k) { this.kids.push(...k); }
          replaceChildren(...k) { this.kids = k; }
          set innerHTML(v) { this.html = v; this.content = new Node("#frag"); this.content.append(v); }
          querySelectorAll() { return []; }
          find(test, out = []) {
            if (test(this)) out.push(this);
            for (const k of this.kids) if (k instanceof Node) k.find(test, out);
            return out;
          }
        }
        globalThis.document = { createElement: t => new Node(t), getElementById: () => null, head: new Node("head") };
        const rec = JSON.parse(process.argv[1]);
        const calls = [], listeners = {};
        const ctx = {
          route: { arg: "Fn_A" }, contract: { run_kinds: ["unit", "set", "tas"], templates: { "run.ended": "" } },
          api: async path => { calls.push(path); return rec; }, on: (kind, fn) => { listeners[kind] = fn; },
        };
        const root = new Node("div");
        await fn.mount(root, ctx);
        const rows = root.find(n => n.tag === "tr" && n.attrs["data-ordinal"] !== undefined).length;
        const links = root.find(n => n.tag === "a").map(n => n.attrs.href);
        const kinds = root.find(n => n.attrs["data-kind"]).map(n => n.attrs["data-kind"]);
        listeners["run.ended"]({ fields: { run: "other", movie: "x" } });
        listeners["run.ended"]({ fields: { run: "other", movie: "menu" } });
        await new Promise(r => setImmediate(r));
        process.stdout.write(JSON.stringify({ rows, links, kinds, calls }));
        """
        record = {
            **{k: BAKED[k] for k in ("name", "file", "first", "size", "state", "movies", "html", "lines")},
            "behaviour": "differs",
            "cases": site_fn.cases_of(BAKED),
            "runs": [
                {**RUN_FIELDS, "id": "u1", "kind": "unit", "stream": False, "first": None, "sides": ["port"]},
                {
                    **RUN_FIELDS,
                    "id": "t1",
                    "kind": "tas",
                    "movie": "menu",
                    "stream": False,
                    "first": 12,
                    "sides": ["port", "original"],
                },
            ],
        }
        script = f'import * as fn from "{FN_JS.as_uri()}"; {fake}'
        done = subprocess.run(
            ["node", "--input-type=module", "-e", script, json.dumps(record)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        got = json.loads(done.stdout)
        self.assertEqual(got["rows"], 4)
        self.assertEqual(got["links"], ["#/stream/t1?poll=12"])
        self.assertEqual(got["kinds"], ["unit", "tas"])
        self.assertEqual(got["calls"], ["/api/fn/Fn_A"] * 2)  # the first load, then one refetch for the menu event

    def test_first_divergence_is_the_first_entry_or_null(self):
        self.assertEqual(self.js('fn.firstDivergence({divergence: [{field:"a"},{field:"b"}]})'), {"field": "a"})
        self.assertIsNone(self.js("fn.firstDivergence({divergence: []})"))


if __name__ == "__main__":
    unittest.main()
