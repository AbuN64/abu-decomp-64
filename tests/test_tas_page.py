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
    "/api/runs",
    "/api/pin",
    "/api/coverage",
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
                "next": "recipe work func_80297310_de",
                "owner": "func_80256C90_de at src/game/TEXTURE/80256220_13.CPP:48",
            },
            {
                "rank": 2,
                "kind": "owes a match",
                "where": "func_80209CD8_de at src/a.CPP",
                "layer": "game source",
                "error": "C unproven, reached by 3 runs",
                "next": "recipe work func_80209CD8_de",
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


class Stub:
    """Serves the page and the contract endpoints from mock data; records POSTs."""

    def __init__(self, state: dict, logs: dict | None = None) -> None:
        self.state, self.logs, self.posts = state, logs or {}, []
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
        fields -= {"frame", "vi_drift_note"}
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
                    stub.url,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
        self.assertEqual(
            dom.count('class="tile'), 1
        )  # the stage follows the newest live run; the other is in the strip
        for text in (
            "model-preview",  # on the stage (newest live run)
            "following live",
            "Take over",
            "boot-idle",
            "TAS-CYCLE-1",
            "Next up",
            "host-fault-00000010",
            "recipe work func_80297310_de",
            "1 take",
            "func_80256C90_de",
            "RW 2574d74ed",
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


if __name__ == "__main__":
    unittest.main()
