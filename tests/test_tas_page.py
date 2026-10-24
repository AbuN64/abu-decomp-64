"""The TAS page against the page contract (orch TAS-PAGE-CONTRACT.md), served by a stub with mock state."""

import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

PAGE = Path(__file__).resolve().parents[1] / "src" / "abu_decomp" / "tas_page.html"
CONTRACT = {
    "/api/state",
    "/api/board",
    "/api/points",
    "/api/runs",
    "/api/pin",
    "/api/coverage",
    "/api/maze",
    "/api/calls/",
    "/fn/",
    "/api/corpus",
    "/api/phase",
    "/api/phase/map",
    "/coverage/",
    "/frame/",
    "/audio/",
    "/log/",
    "/api/play",
    "/api/play-all",
    "/api/stop",
    "/api/take",
    "/api/input",
    "/oracle/",
    "/shot/",
    "/diff/",
}
NOW = 1_800_000_000


def _side(hit):
    return {"hit": hit, "count": int(hit), "movie": "boot-idle" if hit else None, "poll": 12 if hit else None}


def _pct(n):
    return {"functions": n, "functions_pct": 12.5, "bytes": n * 10, "bytes_pct": 20.0}


COVERAGE = {
    "generated": NOW,
    "summary": {
        "version": "us-rev1",
        "movies": 3,
        "oracle_movies": 2,
        "states": {"oracle-only": 1, "neither": 2},
        "game": {"functions": 8, "bytes": 80, "port": _pct(1), "oracle": _pct(2), "either": _pct(2)},
    },
    "gcovr": {"line_percent": 14.9, "branch_percent": 8.1, "function_percent": 17.1},
    "oracle_only": [
        {
            "name": "func_8025_missing_de",
            "file": "src/game/A.CPP",
            "size": 40,
            "port_linked": True,
            "port": _side(False),
            "oracle": _side(True),
        }
    ],
    "neither": [],
    "port_only": [],
}


CORPUS = {
    "entries": [
        {
            "movie": "boot-idle",
            "why": ["seed +300 fn +900 br"],
            "polls": 900,
            "outcome": "ok",
            "baseline": "r1",
            "oracle": "r0",
            "spots": {"rng-seeded": "pass"},
            "verdict": "fail",
            "reasons": ["spot rng-seeded: pass -> fail (value 0)"],
            "first_change": 412,
        }
    ],
    "spots": [
        {
            "id": "rng-seeded",
            "movie": "boot-idle",
            "kind": "ram",
            "expr": "u32@0x80111D24 != 0",
            "at": 600,
            "note": "the RNG is seeded",
            "first": "pass",
        }
    ],
    "features": {"functions": 300, "branches": 900, "ends": 0},
    "gate": {
        "at": NOW - 30,
        "verdict": "fail",
        "counts": {"fail": 1},
        "wall_seconds": 7.1,
        "cpu_seconds": 9.4,
        "build": "abcdef0123",
        "pins": {"rw": "6446a267306585b0"},
        "dropped": [],
        "failed": [{"movie": "boot-idle", "run": "r1", "reasons": ["spot rng-seeded: pass -> fail (value 0)"]}],
    },
    "gating": False,
}


def _phase_fixture() -> tuple[dict, dict]:
    import base64

    cells = bytearray(256)  # order 4: 16 x 16 cells of 8 B over one 2 KB segment
    for i in range(256):
        cells[i] = (2, 3, 4, 6, 7)[i * 5 // 256] | ((i % 11) << 4 if i * 5 // 256 == 4 else 0)
    cells[250:] = b"\x00" * 6
    fns = [
        ["fnA", 0x1000, 400, 3, None, "src/a.c", True],
        ["fnB", 0x1190, 800, 4, None, "src/b.c", False],
        ["fnC", 0x14B0, 600, 7, 62.5, "src/c.c", False],
    ]
    mp = {
        "version": "us-rev1",
        "segments": [[0x1000, 0x1800]],
        "cell_bytes": 8,
        "order": 4,
        "ref": "abc123",
        "blobs": {"mapped": 600, "bytes": 1000},
        "cells": base64.b64encode(bytes(cells)).decode(),
        "fns": fns,
        "files": [["src/a.c", 0, 49]],
    }
    row = lambda ph, key, value, thr, met: {  # noqa: E731
        "phase": ph,
        "game": "RageWars",
        "version": "us-rev1",
        "key": key,
        "value": value,
        "threshold": thr,
        "met": met,
        "source": "board",
        "age_s": 30,
    }
    phase = {
        "generated": NOW,
        "focus": {"game": "RageWars", "version": "us-rev1"},
        "rows": [
            row(1, "p1.mapped_pct", 100.0, 100, True),
            row(1, "p1.foundations_named_pct", 41.5, 100, False),
            row(2, "p2.RageWars.port", True, True, True),
            row(3, "p3.fn_reachable_pct", 31.9, 100, False),
            row(4, "p4.byte_reach_pct", 42.5, 50, False),
        ],
        "stage": None,
        "next_stage": {"stage": "R1", "failing": [{"key": "byte_reach", "value": 42.5, "threshold": 50}]},
        "next": [{"phase": 1, "fn": "fnA", "why": "not named", "next": "decomp name fnA"}],
    }
    return phase, mp


def mock_state() -> dict:
    movie = lambda name, **kw: {  # noqa: E731
        "name": name,
        "polls": 900,
        "outcome": "ok",
        "subject": None,
        "reached": 900,
        "same_twice": True,
        "fn": None,
        "train": None,
        "last_run": NOW - 75,
        "label": None,
        "oracle_max": 0.012,
        "root": name,
        "from": None,
        "at": None,
        "kind": None,
        **kw,
    }
    run = lambda rid, name, **kw: {  # noqa: E731
        "id": rid,
        "movie": name,
        "status": "running",
        "speed": 0,
        "label": None,
        "port": "/rig/tas/build/ragewars",
        "poll": 300,
        "polls": 900,
        "outcome": None,
        "subject": None,
        "started": NOW - 5,
        "ended": None,
        "input": "movie",
        "took_at": None,
        "stopped": False,
        "from": None,
        "at": None,
        "saved": None,
        "pinned": False,
        "pak": "on",
        "cpak": "none",
        "fps_unlock": True,
        "shown": {"presented_per_s": 118.4, "game_per_s": 30.0},
        "oracle": {
            "frame": f"/oracle/{rid}",
            "status": "done",
            "poll": 1200,
            "outcome": "ok",
            "subject": None,
            "every": 25,
            "diff": 0.021,
            "diff_poll": 275,
            "max_diff": 0.4375,
            "max_poll": 150,
            "drift": 0,
            "vi_drift": 3,
            "fps": 912.0,
        },
        **kw,
    }
    return {
        "now": NOW,
        "build": {"port": "/rig/tas/build/ragewars", "pin": "2574d74ed0c1", "built": NOW - 600},
        "runs": [
            run("r1", "boot-idle"),
            run(
                "r2",
                "model-preview",
                speed=1,
                poll=120,
                polls=1400,
                started=NOW - 4,
                label="TAS-CYCLE-1",
                port="/rig/lanes/TAS-CYCLE-1/build/ragewars",
            ),
            run(
                "r3",
                "title-to-player-select",
                status="done",
                outcome="crash",
                poll=611,
                polls=1200,
                subject="func_80256C90_de+0x10",
                started=NOW - 30,
                ended=NOW - 20,
                input="human",
                took_at=400,
                saved="title-to-player-select-take-20261023-010101",
                pinned=True,
                oracle=None,
            ),
        ],
        "movies": [
            movie("boot-idle"),
            movie(
                "model-preview",
                polls=1400,
                outcome="crash",
                subject="func_80256C90_de+0x10",
                reached=611,
                same_twice=False,
                fn={"name": "func_80256C90_de", "state": "fuzzy"},
                train={"line": "final-80256C90.diff", "mark": None},
            ),
            movie(
                "title-to-player-select",
                polls=1200,
                outcome="crash",
                subject="func_80256C90_de+0x10",
                reached=611,
                fn={"name": "func_80256C90_de", "state": "exact"},
                train={"line": "final-80256C90.diff", "mark": "LANDED"},
            ),
            movie("boot-missing", outcome="hang", reached=328, same_twice=None, last_run=None),
            movie(
                "title-to-player-select-take-20261023-010101",
                root="title-to-player-select",
                **{"from": "title-to-player-select", "at": 400},
            ),
        ],
        "lanes": [
            {
                "name": "TAS-CYCLE-1",
                "title": "copyright splash off center",
                "kind": "cycle",
                "status": "17:47 start",
                "status_at": NOW - 40,
                "result": None,
                "runs": 1,
            },
            {
                "name": "TAS-CYCLE-0",
                "title": "old cycle",
                "kind": "cycle",
                "status": "17:00 done",
                "status_at": NOW - 4000,
                "result": "DONE: splash centred",
                "runs": 0,
            },
        ],
        "inbox": [
            {"line": "final-80256C90.diff  func_80256C90_de", "mark": "LANDED"},
            {"line": "final-80209CD8.diff  func_80209CD8_de", "mark": None},
        ],
    }


BOARD = {
    "decomp": {
        "generated": NOW - 100,
        "head": "ba2d6977ff76",
        "head_subject": "Update progress",
        "head_time": NOW - 120,
        "versions": [
            {
                "name": "us-rev1",
                "exact_pct": 90.72,
                "fuzzy_pct": 100.0,
                "bytes": 1370404,
                "code": {"exact": 1002424, "fuzzy": 127100, "handwritten": 0, "none": 0},
                "functions": {"exact": 4183, "fuzzy": 162, "none": 0},
                "none_fns": [],
                "bridge": {"linked": True, "booted": True, "units_in": 71, "units_out": 73},
                "spark": [[NOW - 900, 90.1, 100.0], [NOW - 100, 90.72, 100.0]],
            }
        ],
    },
    "landed": [{"sha": "718492a18aa", "time": NOW - 300, "subject": "Match func_802013E0_de"}],
    "triage": {
        "generated": NOW - 20,
        "error": None,
        "rows": [
            {
                "rank": 1,
                "key": "datum D_800E42D8",
                "level": "datum",
                "title": "datum D_800E42D8 [unsigned int D_800E42D8[35]] touched at +0x2",
                "count": 8,
                "movies": ["model-preview", "boot-idle"],
                "sites": 8,
                "kinds": {"crash": 7, "sweep": 1},
                "offsets": [2],
                "datum": "D_800E42D8",
                "fn": "func_8041F2A0_us_rev1",
                "callers": ["func_80402000_de"],
                "site": "src/game/DATA/OVLTBL_8/800E42D8.CPP:3",
                "declared": "unsigned int D_800E42D8[35]",
                "members": [{"kind": "crash", "movie": "model-preview", "poll": 40, "run": "r1", "evidence": "/r/r1"}],
                "next": "decomp --profile p.json tas play model-preview --speed 1",
            }
        ],
    },
    "work": {
        "generated": NOW - 30,
        "error": None,
        "rows": [
            {
                "rank": 1,
                "kind": "host fault",
                "where": "host-fault-00000010",
                "blocks": ["model-preview"],
                "layer": "game source",
                "error": "crash host-fault-00000010",
                "next": "decomp arity show func_80297310_de",
                "owner": "func_80256C90_de at src/game/TEXTURE/80256220_13.CPP:48",
            },
            {
                "rank": 2,
                "kind": "owes a match",
                "where": "func_80209CD8_de at src/a.CPP",
                "layer": "game source",
                "error": "C unproven, reached by 3 runs",
                "next": "decomp arity show func_80209CD8_de",
            },
        ],
    },
}


def bmp(width: int = 320, height: int = 240, rgb: tuple = (40, 90, 140)) -> bytes:
    row = bytes(rgb[::-1]) * width
    pixels = row * height
    head = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return head + info + pixels


MAZE = {
    "generated": 1,
    "version": "us-rev1",
    "exact": True,
    "total": 4,
    "covered": 3,
    "real_c": 3,
    "covered_pct": 75.0,
    "real_pct": 75.0,
    "modules": [
        {"name": "game/a", "cells": [["func_A_de", 16, "exact", 1, 0], ["func_B_de", 32, "fuzzy", 1, 1]]},
        {"name": "game/b", "cells": [["func_C_de", 8, "none", 1, 1], ["func_D_de", 8, "exact", 0, -1]]},
    ],
    "replays": [
        {"name": "movie-one", "start": "power-on", "status": "ok", "polls": 9, "cells": [0, 1]},
        {"name": "movie-two", "start": "boot", "status": "crash", "polls": 3, "cells": [1, 2]},
    ],
    "movies": ["movie-one", "movie-two"],
}
CALLS = {
    "name": "func_A_de",
    "self": {"name": "func_A_de", "size": 16, "state": "exact", "covered": True, "by_movie": None},
    "callers": [{"name": "func_Caller_de", "size": 4, "state": "fuzzy", "covered": True, "by_movie": None}],
    "callees": [{"name": "func_Callee_de", "size": 4, "state": "none", "covered": False, "by_movie": None}],
    "path": ["func_Caller_de", "func_A_de"],
    "path_kind": "static (jal targets of the ELF)",
    "graph": True,
}


class Stub:
    """Serves the page and the contract endpoints from mock data; records POSTs."""

    def __init__(self, state: dict, logs: dict | None = None, phase: bool = True) -> None:
        self.state, self.logs, self.posts = state, logs or {}, []
        self.phase, self.map = _phase_fixture() if phase else (None, None)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body: bytes, kind: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/":
                    self.reply(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self.reply(json.dumps(stub.state).encode(), "application/json")
                elif path == "/api/coverage":
                    self.reply(json.dumps(COVERAGE).encode(), "application/json")
                elif path == "/api/phase" and stub.phase:
                    self.reply(json.dumps(stub.phase).encode(), "application/json")
                elif path == "/api/phase/map" and stub.map:
                    self.reply(json.dumps(stub.map).encode(), "application/json")
                elif path == "/api/maze":
                    self.reply(json.dumps(MAZE).encode(), "application/json")
                elif path.startswith("/api/calls/"):
                    self.reply(json.dumps(CALLS).encode(), "application/json")
                elif path == "/api/corpus":
                    self.reply(json.dumps(CORPUS).encode(), "application/json")
                elif path == "/api/board":
                    self.reply(json.dumps(BOARD).encode(), "application/json")
                elif path == "/api/runs":
                    done = [r for r in stub.state["runs"] if r["status"] == "done"]
                    self.reply(json.dumps({"runs": done, "more": False}).encode(), "application/json")
                elif path.startswith(("/frame/", "/oracle/")):
                    self.reply(bmp(), "image/bmp")
                elif path.startswith("/shot/"):
                    side = "oracle" if "side=oracle" in query else "port"
                    self.reply(bmp(rgb=(40, 90, 140) if side == "port" else (240, 240, 240)), "image/bmp")
                elif path.startswith("/diff/"):
                    rows = [
                        {"poll": p, "diff": 0.02, "drift": 25 if p == 150 else 0, "vi_drift": 3} for p in (25, 150, 275)
                    ]
                    self.reply(json.dumps({"every": 25, "rows": rows}).encode(), "application/json")
                elif path.startswith("/log/"):
                    text = stub.logs.get(path[5:], b"")
                    start = int(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("from", 0))
                    self.reply(text[start:], "text/plain; charset=utf-8")
                else:
                    self.reply(b"", "text/plain", 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                stub.posts.append((self.path, body))
                self.reply(json.dumps({"id": "new", "ids": []}).encode(), "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def script() -> str:
    return PAGE.read_text().split("<script>", 1)[1].split("</script>", 1)[0]


class PageTests(unittest.TestCase):
    def test_page_is_one_self_contained_file(self):
        html = PAGE.read_text()
        self.assertNotRegex(html, r"https?://")
        self.assertNotRegex(html, r"<script[^>]+src=|<link[^>]+href=|@import")

    def test_page_calls_only_contract_endpoints(self):
        used = set(re.findall(r'["`](/[a-z\-/]*)', script()))
        self.assertTrue(used, "no endpoints found")
        self.assertLessEqual(used, CONTRACT)
        self.assertLessEqual(CONTRACT, used)

    def test_page_renders_every_contract_field(self):
        js = script()
        state = mock_state()
        fields = set(state["runs"][0]) | set(state["movies"][0]) | set(state["build"]) | set(state["lanes"][0])
        fields |= set(state["runs"][0]["oracle"]) | set(state["inbox"][0])
        version = BOARD["decomp"]["versions"][0]
        fields |= set(BOARD) | set(BOARD["decomp"]) - {"head_subject", "head_time"} | set(version) - {"bytes"}
        fields |= set(BOARD["work"]) | set(BOARD["work"]["rows"][0]) | set(BOARD["landed"][0])
        fields |= set(CORPUS) | set(CORPUS["entries"][0]) - {"reasons"} | set(CORPUS["spots"][0])
        fields |= set(CORPUS["gate"]) | set(CORPUS["gate"]["failed"][0])
        fields |= set(BOARD["triage"]) | set(BOARD["triage"]["rows"][0])
        fields -= {"frame", "vi_drift_note", "ends", "key", "kinds", "offsets", "datum", "declared"}
        for name in sorted(fields | {"state", "more"}):
            self.assertRegex(js, rf"\.{name}\b", name)

    def test_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.js"
            path.write_text(script())
            subprocess.run([node, "--check", str(path)], check=True, timeout=30)

    def test_stub_serves_page_and_state(self):
        stub = Stub(mock_state())
        self.addCleanup(stub.close)
        self.assertIn(b"<title>TAS</title>", urlopen(stub.url, timeout=5).read())
        self.assertEqual(json.load(urlopen(stub.url + "api/state", timeout=5))["now"], NOW)

    def test_headless_render_shows_the_stage_strip_and_rail(self):
        chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not chrome:
            self.skipTest("no chromium")
        stub = Stub(mock_state(), {"r2": b"boot ok\nstep 3 start\n"})
        self.addCleanup(stub.close)
        with tempfile.TemporaryDirectory() as profile:
            dom = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--window-size=1920,1080",
                    "--virtual-time-budget=3000",
                    "--dump-dom",
                    stub.url + "#work",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        self.assertEqual(
            dom.count('class="tile'), 2
        )  # the wall shows every running run (the stage's too), one tile each
        for text in (
            "model-preview",  # on the stage (newest live run)
            "following live",
            "Take over",
            "boot-idle",
            "TAS-CYCLE-1",
            "Next up",
            "host-fault-00000010",
            "decomp arity show func_80297310_de",
            "1 take",
            "func_80256C90_de",
            "RW 2574d74ed",
            "Regression corpus",  # Work tab: the corpus, its last gate and problem spots
            "gate fail",
            "rng-seeded",
            "decomp tas corpus accept boot-idle",
        ):
            self.assertIn(text, dom, text)
        self.assertRegex(dom, r'id="conn" class="dot ok"')  # state fetched

    def test_headless_render_of_each_rail_tab(self):
        chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not chrome:
            self.skipTest("no chromium")
        stub = Stub(mock_state())
        self.addCleanup(stub.close)
        expect = {
            "decomp": ("us-rev1", "90.72%", "Match func_802013E0_de", "LANDED", "units in"),
            "lanes": ("copyright splash off center", "17:47 start", "watch 1"),
            "runs": ("title-to-player-select", "taken @400", "★"),
            "work": ("Buckets", "D_800E42D8", "blocks 2 movies", "func_8041F2A0_us_rev1 <- func_80402000_de"),
        }
        for tab, texts in expect.items():
            with tempfile.TemporaryDirectory() as profile:
                argv = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run"]
                argv += [f"--user-data-dir={profile}", "--window-size=1600,1000", "--virtual-time-budget=4000"]
                dom = subprocess.run(
                    [*argv, "--dump-dom", stub.url + f"#{tab}"], capture_output=True, text=True, timeout=60
                ).stdout
            for text in texts:
                self.assertIn(text, dom, f"{tab}: {text}")

    def _dom(self, stub, hash_, size="1600,1000"):
        chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not chrome:
            self.skipTest("no chromium")
        with tempfile.TemporaryDirectory() as profile:
            argv = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run"]
            argv += [f"--user-data-dir={profile}", f"--window-size={size}", "--virtual-time-budget=4000"]
            return subprocess.run(
                [*argv, "--dump-dom", stub.url + hash_], capture_output=True, text=True, timeout=60
            ).stdout

    def test_page_has_the_wall_map_and_film_hooks(self):
        html = PAGE.read_text()
        for needle in (
            'id="strip" class="wall"',
            'id="p-map"',
            '"map", "Map"',
            "WALL_MAX = 12",
            "WALL_MS = 500",
            "/api/phase/map",
            "phase data not deployed yet",
            'dataset.top = "film"',
            'data-top="film"',
        ):
            self.assertIn(needle, html, needle)
        self.assertLess(html.index('["map", "Map"]'), html.index('["work", "Work"]'))  # Map is the first tab

    def test_hilbert_mapping_is_the_standard_curve(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        js = script()
        a, b = js.index("function d2xy"), js.index("async function pollPhase")
        check = (
            js[a:b]
            + "const n = 16; const seen = new Set(); let prev = null;\n"
            + "for (let d = 0; d < n * n; d++) { const [x, y] = d2xy(n, d); seen.add(x + ',' + y);\n"
            + "  if (xy2d(n, x, y) !== d) throw new Error('inverse ' + d);\n"
            + "  if (prev && Math.abs(prev[0] - x) + Math.abs(prev[1] - y) !== 1) throw new Error('gap ' + d);\n"
            + "  prev = [x, y]; }\n"
            + "if (seen.size !== n * n) throw new Error('not a bijection');\n"
            + "const q = [0, 1, 2, 3].map(d => d2xy(2, d).join());\n"
            + "if (q.join('|') !== '0,0|0,1|1,1|1,0') throw new Error('order-1 curve ' + q);\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hilbert.js"
            path.write_text(check)
            subprocess.run([node, str(path)], check=True, timeout=30)

    def test_headless_map_tab_shows_gates_ladder_and_canvas(self):
        stub = Stub(mock_state())
        self.addCleanup(stub.close)
        dom = self._dom(stub, "#map")
        shown = ('id="mapc"', "Phase 1", "p1.foundations_named_pct", "41.5 / 100")
        for text in (*shown, "R1 needs", "byte_reach 42.5 / 50", "below R1"):
            self.assertIn(text, dom, text)
        self.assertNotIn('class="empty">phase data not deployed yet', dom)

    def test_headless_map_tab_degrades_when_phase_routes_are_missing(self):
        stub = Stub(mock_state(), phase=False)
        self.addCleanup(stub.close)
        dom = self._dom(stub, "#map")
        self.assertIn('class="empty">phase data not deployed yet', dom)
        self.assertIn("boot-idle", dom)  # the rest of the page still works

    def test_headless_film_hides_the_chrome(self):
        stub = Stub(mock_state())
        self.addCleanup(stub.close)
        dom = self._dom(stub, "#film", "1920,1080")
        self.assertIn('data-top="film"', dom)
        self.assertIn('id="mapc"', dom)
        self.assertIn('class="tile', dom)

    def test_headless_render_of_the_maze_and_its_call_tree(self):
        chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not chrome:
            self.skipTest("no chromium")
        stub = Stub(mock_state())
        self.addCleanup(stub.close)
        for hash_, texts in {
            "maze": ("75.0% covered", "movie-one", "movie-two", "only here", "real C 75%"),
            "maze:func_A_de": ("Call tree", "func_Caller_de", "func_Callee_de", "static path: func_Caller_de"),
        }.items():
            with tempfile.TemporaryDirectory() as profile:
                argv = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run"]
                argv += [f"--user-data-dir={profile}", "--window-size=390,900", "--virtual-time-budget=4000"]
                dom = subprocess.run(
                    [*argv, "--dump-dom", stub.url + f"#{hash_}"], capture_output=True, text=True, timeout=60
                ).stdout
            for text in texts:
                self.assertIn(text, dom, f"{hash_}: {text}")


if __name__ == "__main__":
    unittest.main()
