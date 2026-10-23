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
    "/frame/",
    "/audio/",
    "/log/",
    "/api/play",
    "/api/play-all",
    "/api/stop",
    "/api/take",
    "/api/input",
}
NOW = 1_800_000_000


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
        "frame": f"/frame/{rid}",
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
        ],
        "cycle": {
            "lanes": [
                {
                    "name": "TAS-CYCLE-1",
                    "title": "copyright splash off center",
                    "status": "17:47 start",
                    "status_at": NOW - 40,
                    "result": None,
                    "runs": 1,
                },
                {
                    "name": "TAS-CYCLE-0",
                    "title": "old cycle",
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
                elif path.startswith("/frame/"):
                    self.reply(bmp(), "image/bmp")
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
        fields = set(state["runs"][0]) | set(state["movies"][0]) | set(state["cycle"]) | set(state["build"])
        fields |= set(state["cycle"]["lanes"][0])
        fields -= {"frame"}  # the page builds /frame/<id> from the id
        for name in sorted(fields | {"state", "mark", "line"}):
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

    def test_headless_render_shows_tiles_movies_inbox_and_log(self):
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
        self.assertEqual(dom.count('class="tile'), 3)  # two running + the crash still fresh
        for text in (
            "boot-idle",
            "boot-missing",
            "func_80256C90_de",
            "LANDED",
            "DIFF",
            "fuzzy",
            "turbo",
            "1x",
            "TAS-CYCLE-1",
            "copyright splash off center",
            "DONE: splash centred",
            "step 3 start",
            "crash",
            "611/1200",
            "2574d74ed",
        ):
            self.assertIn(text, dom, text)
        self.assertRegex(dom, r"id=\"conn\" class=\"dim\"></span>")  # state fetched


if __name__ == "__main__":
    unittest.main()
