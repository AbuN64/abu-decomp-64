# ruff: noqa: E501  (the embedded page-script probes are one line each)
"""The Runner page (runner.html) against its contract (orch plans/RUNNER-CONTRACT.md), served by a stub with baked sample data.

Pure parts of the page script (frame framing, reach decode, baked-file lookup, pad state) run under node; the rendered page
runs in headless Chromium driven over CDP by tests/data/runner/drive.mjs.
"""

import base64
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

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "src" / "abu_decomp" / "runner.html"
STATIC = ROOT / "src" / "abu_decomp" / "static"
DRIVE = Path(__file__).with_name("data") / "runner" / "drive.mjs"
NOW = 1_800_000_000
# every path the page may request (prefixes end in "/"); the contract lists the same set
CONTRACT = {
    "/static/uFuzzy.iife.min.js",
    "/static/icons.svg",
    "/data/live.json",
    "/data/manifest.json",
    "/data/movies.json",
    "/data/summary.json",
    "/data/posters/",
    "/data/reach/",
    "/data/fn/",
    "/data/",
    "/wall",
    "/video/",
    "/frame/",
    "/audio/",
    "/diff/",
    "/shot/",
    "/api/play",
    "/api/take",
    "/api/stop",
    "/api/input",
    "/api/pin",
}
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCAAGAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCrRRRXMe2f/9k="
)

STATES = ["named", "exact", "fuzzy", "asm"]
FILES = ["src/game/MENU/menu.c", "src/game/ENGINE/engine.c", "src/sdk/libultra/a.c"]


def fn_rows():
    rows = []
    for i in range(30):
        name = {0: "Menu_Draw", 1: "Engine_Tick", 2: "Gfx_Init"}.get(i, f"func_8000{i:04X}")
        reach = "both" if i < 3 else ("port" if i < 9 else "none")
        rows.append([name, 0x1000 + i * 64, 700 + i, STATES[i % 4], i % 3, reach, i == 1])
    return rows


FNS = {"version": "us-rev1", "ref": "abc", "files": FILES, "rows": fn_rows()}
RUN_LIVE = {
    "id": "r-live",
    "movie": "boot-idle",
    "status": "running",
    "speed": 0,
    "poll": 100,
    "polls": 900,
    "outcome": None,
    "subject": None,
    "started": NOW - 30,
    "ended": None,
    "label": "t",
    "input": "movie",
    "took_at": None,
    "fps_unlock": False,
    "oracle": {"frame": 1},
    "pinned": False,
}
RUN_DONE = dict(
    RUN_LIVE,
    id="r-done",
    movie="menu-crash",
    status="done",
    poll=300,
    polls=300,
    outcome="crash",
    ended=NOW - 5,
    oracle={"status": "done"},
)
LIVE = {
    "now": NOW,
    "build": {"port": "/p", "pin": "0123456789abcdef", "built": NOW - 90, "format": 1},
    "runs": [RUN_LIVE, RUN_DONE],  # serve class Live: running, queued and newest finished runs in one list
}
MOVIES = {
    "cols": ["name", "polls", "outcome", "fn", "last_run", "kind", "run"],
    "rows": [
        ["boot-idle", 900, "ok", None, NOW - 10, None, "r-live"],
        ["menu-crash", 300, "crash", "Menu_Draw", NOW - 50, None, "r-done"],
    ],
}
SUMMARY = {
    "version": "us-rev1",
    "generated": NOW,
    "game": {"either": {"functions_pct": 51.4}},
    "behaviour": {"behaves": 108},
    "gcovr": {"line_percent": 46.4, "branch_percent": 36.5},
    "stage": None,
    "next_stage": {"stage": "R1", "failing": [{"key": "line", "value": 33.6, "threshold": 40}]},
}
FN_FILE = {
    "name": "Menu_Draw",
    "first": 10,
    "html": ['<span class="kt">void</span> Menu_Draw(void) {', "  Engine_Tick();", "}"],
    "lines": [[5], [5, 1, 2], [0]],
    "callers": ["Engine_Tick"],
    "callees": ["Gfx_Init"],
    "movies": ["boot-idle"],
    "movies_total": 1,
    "port": {"count": 7},
    "oracle": {"count": 3},
    "behaviour": "differs",
    "claims": [],
    "behave": ["Menu_Draw(a0=1) -> v0=0"],
}


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def reach_full(poll, rows):
    bits = bytearray(4)
    for r in rows:
        bits[r >> 3] |= 1 << (r & 7)
    return b"\x01" + struct.pack("<I", poll) + bytes(bits)


def reach_delta(poll, fresh, hot):
    out = b"\x02" + struct.pack("<I", poll)
    for lst in (fresh, hot):
        out += varint(len(lst))
        prev = 0
        for v in sorted(lst):
            out += varint(v - prev)
            prev = v
    return out


def frame(body, seq=0, reach=False):
    return struct.pack(">IQI", len(body) | (0x80000000 if reach else 0), 123, seq) + body


class Stub:
    """Serves the page, its static files, baked data and the streams from fixtures; records POSTs."""

    def __init__(self, release=True, input_human=False):
        self.posts, self.gets, self.release = [], [], release
        stub = self
        live = dict(LIVE, runs=[dict(RUN_LIVE, input="human" if input_human else "movie"), RUN_DONE])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, body, kind, status=200):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def js(self, value, status=200):
                self.reply(json.dumps(value).encode(), "application/json", status)

            def do_GET(self):
                path = self.path.partition("?")[0]
                stub.gets.append(path)
                if path == "/":
                    self.reply(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path.startswith("/static/") and (STATIC / path[8:]).is_file():
                    kind = "image/svg+xml" if path.endswith(".svg") else "text/javascript"
                    self.reply((STATIC / path[8:]).read_bytes(), kind)
                elif path == "/data/live.json":
                    self.js(live)
                elif path == "/data/manifest.json":
                    self.js({"fns": "fns.abc123def.json", "fnrows": "fnrows.abc123def.txt"})
                elif path == "/data/fns.abc123def.json":
                    self.js(FNS)
                elif path == "/data/movies.json":
                    self.js(MOVIES)
                elif path == "/data/summary.json":
                    self.js(SUMMARY)
                elif path == "/data/fn/Menu_Draw.json":
                    self.js(FN_FILE)
                elif path.startswith("/data/reach/"):
                    self.reply(bytes([0xFF, 0x0F, 0, 0]), "application/octet-stream")
                elif path.startswith("/data/posters/") or path in ("/frame/r-done", "/frame/r-live"):
                    self.reply(JPEG, "image/jpeg")
                elif path == "/video/r-live":
                    body = frame(JPEG, 1) + frame(reach_full(10, [0, 1, 2]), 1, True) + frame(JPEG, 2)
                    body += frame(reach_delta(15, [5], [1, 5]), 2, True)
                    self.reply(body, "application/x-tas-frames")
                elif path == "/wall":  # index 7 named by its table, then the running run's thumb and reach record
                    table = json.dumps({"id": "r-live"}).encode()
                    body = frame(table, 7, True) + frame(JPEG, 7) + frame(reach_full(10, [0, 3]), 7, True)
                    self.reply(body, "application/x-tas-frames")
                elif path == "/diff/r-done":
                    self.js({"every": 100, "rows": [{"poll": 100, "diff": 0.0}, {"poll": 200, "diff": 0.25}]})
                elif path == "/shot/r-done":
                    self.reply(JPEG, "image/jpeg")
                else:
                    self.js({"error": "not found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                stub.posts.append((self.path, body))
                if self.path == "/api/take":
                    self.js({"id": body["id"], "taken": True})
                elif self.path == "/api/input" and body.get("release"):
                    self.js({"ok": True, "released": True} if stub.release else {"ok": True})
                elif self.path == "/api/play":
                    self.js({"id": "r-new", "movie": body.get("movie") or "owner"})
                else:
                    self.js({"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def script():
    return re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)


def pure():
    return re.search(r"/\* pure:begin.*?\*/\n(.*?)/\* pure:end \*/", script(), re.S).group(1)


def chrome():
    return shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


class PageFileTests(unittest.TestCase):
    def test_page_has_no_external_references(self):
        html = PAGE.read_text()
        self.assertNotRegex(html, r"https?://")
        self.assertNotRegex(html, r"<link[^>]+href=\"(?!data:)|@import")
        self.assertEqual(re.findall(r'<script[^>]+src="([^"]+)"', html), ["/static/uFuzzy.iife.min.js"])

    def test_old_pages_are_gone(self):
        src = PAGE.parent
        self.assertFalse((src / "tas_page.html").exists())
        self.assertFalse((src / "tas_fn.html").exists())

    def test_static_files_and_licences(self):
        for name in ("uFuzzy.iife.min.js", "icons.svg", "LICENSE.ufuzzy.txt", "LICENSE.lucide.txt"):
            self.assertTrue((STATIC / name).is_file(), name)
        sprite = (STATIC / "icons.svg").read_text()
        self.assertEqual(len(re.findall(r"<symbol ", sprite)), 35)
        self.assertEqual(set(re.findall(r'stroke-width="([\d.]+)"', sprite)), {"1.75"})
        used = set(re.findall(r'icon\("([a-z0-9-]+)"', script())) | set(
            re.findall(r'<use href="#i-([a-z0-9-]+)"', PAGE.read_text())
        )
        have = set(re.findall(r'symbol id="i-([a-z0-9-]+)"', sprite))
        self.assertLessEqual(used - {"x"}, have, sorted(used - have))

    def test_page_calls_only_contract_paths(self):
        used = set(
            re.findall(
                r'["`](/(?:static|data|api|wall|video|frame|audio|diff|shot)[A-Za-z0-9_.\-/]*)',
                script() + PAGE.read_text(),
            )
        )
        norm = set(used)
        for u in norm:
            self.assertTrue(u in CONTRACT or any(c.endswith("/") and u.startswith(c) for c in CONTRACT), u)
        for c in CONTRACT:
            self.assertTrue(any(u.startswith(c) for u in norm), f"contract path not used: {c}")

    def test_deleted_routes_are_not_used(self):
        js = script()
        for gone in (
            "/api/state",
            "/api/board",
            "/api/corpus",
            "/api/points",
            "/api/maze",
            "/api/fn",
            "/api/calls",
            "/api/phase",
            "/api/runs",
            "/api/play-all",
            "/api/pool",
        ):
            self.assertNotIn(gone, js, gone)

    def test_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.js"
            path.write_text(script())
            subprocess.run([node, "--check", str(path)], check=True, timeout=30)


class PureTests(unittest.TestCase):
    def run_node(self, body):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pure.js"
            path.write_text("const assert = require('node:assert/strict');\n" + pure() + "\n" + body)
            subprocess.run([node, str(path)], check=True, timeout=30)

    def test_framer_splits_frames_across_chunks_and_flags_reach(self):
        a, b = frame(b"JPEG1", 4), frame(b"\x01xxxx", 5, True)
        data = base64.b64encode(a + b).decode()
        self.run_node(f"""
const all = Buffer.from("{data}", "base64"), got = [];
const feed = makeFramer(r => got.push([r.reach, r.seq, Buffer.from(r.body).toString("latin1")]));
for (let i = 0; i < all.length; i += 7) feed(new Uint8Array(all.subarray(i, i + 7)));
assert.deepEqual(got, [[false, 4, "JPEG1"], [true, 5, "\\x01xxxx"]]);
assert.throws(() => makeFramer(() => {{}})(new Uint8Array([0x7f, 0xff, 0xff, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])), /invalid frame length/);
""")

    def test_reach_full_then_delta_updates_bits_fresh_and_hot(self):
        full, delta = reach_full(10, [0, 1, 2]), reach_delta(15, [5, 9], [1, 5])
        f64, d64 = base64.b64encode(full).decode(), base64.b64encode(delta).decode()
        self.run_node(f"""
const F = {{ bits: null, n: 0, poll: 0, hot: new Map(), fresh: new Map(), bytes: 0, recs: 0, maxRec: 0 }};
assert.equal(applyReach(F, new Uint8Array(Buffer.from("{f64}", "base64")), 1), true);
assert.equal(F.n, 3); assert.equal(F.poll, 10);
assert.equal(applyReach(F, new Uint8Array(Buffer.from("{d64}", "base64")), 2), true);
assert.equal(F.n, 5); assert.equal(F.poll, 15);
assert.deepEqual([...F.fresh.keys()].sort((a, b) => a - b), [5, 9]);
assert.deepEqual([...F.hot.keys()].sort((a, b) => a - b), [1, 5, 9]);  // a fresh row is also a grown row
assert.equal(F.recs, 2);
const G = {{ bits: null, n: 0, hot: new Map(), fresh: new Map(), bytes: 0, recs: 0, maxRec: 0 }};
assert.equal(applyReach(G, new Uint8Array(Buffer.from("{d64}", "base64")), 1), false);  // a delta before any full record is ignored
""")

    def test_diff_pixels_marks_only_pixels_past_the_threshold(self):
        self.run_node("""
const d = new Uint8ClampedArray([10, 10, 10, 255, 200, 0, 0, 255]), o = new Uint8ClampedArray([12, 10, 10, 255, 0, 0, 0, 255]);
assert.equal(diffPixels(d, o, 48), 0.5);
assert.deepEqual([...d], [2, 2, 2, 255, 255, 0, 200, 255]);  // same pixel dims, differing pixel turns magenta
""")

    def test_data_path_reads_the_manifest(self):
        self.run_node("""
assert.equal(dataPath({ fns: "fns.abc123.json" }, "fns", "fns.json"), "/data/fns.abc123.json");
assert.equal(dataPath({ files: { fns: "fns.abc123.json" } }, "fns", "fns.json"), "/data/fns.abc123.json");
assert.equal(dataPath({ files: ["movies.json", "fns.0a1b2c3d.json", "fnrows.0a1b2c3d.txt"] }, "fns", "fns.json"), "/data/fns.0a1b2c3d.json");
assert.equal(dataPath({}, "fns", "fns.json"), "/data/fns.json");
assert.equal(dataPath(null, "fns", "fns.json"), "/data/fns.json");
""")

    def test_pad_state_maps_keys_gamepad_and_touch(self):
        self.run_node("""
const none = { buttons: 0, x: 0, y: 0 };
assert.deepEqual(padState(new Set(["KeyX", "ArrowUp", "ArrowRight"]), none, []), { buttons: 0x8000, x: 80, y: 80 });
assert.deepEqual(padState(new Set(), { buttons: 0x1000, x: 200, y: -5 }, []), { buttons: 0x1000, x: 80, y: -5 });
const pad = { buttons: Array.from({ length: 16 }, (_, i) => ({ pressed: i === 0 || i === 9 })), axes: [1, 0, 0, 0] };
assert.deepEqual(padState(new Set(), none, [pad]), { buttons: 0x8000 | 0x1000, x: 80, y: 0 });
""")


@unittest.skipUnless(chrome() and shutil.which("node"), "needs chromium and node")
class RenderedPageTests(unittest.TestCase):
    def drive(self, stub, hash_, steps, size=(1440, 900)):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "url": stub.url + hash_,
                        "width": size[0],
                        "height": size[1],
                        "chrome": chrome(),
                        "profile": str(Path(tmp) / "p"),
                        "steps": steps,
                    }
                )
            )
            out = subprocess.run(
                ["node", "--experimental-websocket", str(DRIVE), str(plan)],
                capture_output=True,
                text=True,
                timeout=90,
                check=True,
            ).stdout
        data = json.loads(out)
        self.assertEqual(data["errors"], [])
        return data

    def stub(self, **kw):
        s = Stub(**kw)
        self.addCleanup(s.close)
        return s

    def test_grid_shows_live_recent_and_movies(self):
        s = self.stub()
        d = self.drive(
            s,
            "",
            [
                {"wait": 1500},
                {
                    "eval": "[document.querySelectorAll('.tile[data-run]').length, document.querySelectorAll('.tile[data-movie]').length, document.querySelectorAll('img[data-wall]').length, document.title, document.getElementById('build').textContent]"
                },
            ],
        )
        self.assertEqual(d["results"][0], [2, 2, 1, "Runner", "01234567"])
        self.assertIn("/wall", s.gets)

    def test_first_view_request_budget(self):
        s = self.stub()
        d = self.drive(s, "", [{"wait": 1500}])
        before_pictures = [
            r for r in d["requests"] if not re.match(r"(/(data/posters|wall|video|frame)|blob:)", r["url"])
        ]
        # the page, uFuzzy, the sprite, live.json and the manifest come first; fns, movies and summary follow the first paint
        self.assertLessEqual(len(before_pictures), 8, [r["url"] for r in before_pictures])

    def test_watch_lights_the_grid_from_reach_records_in_the_video_stream(self):
        s = self.stub()
        d = self.drive(
            s,
            "#/w/r-live",
            [
                {"wait": 2000},
                {"eval": "document.getElementById('feedtxt').textContent"},
                {"eval": "document.getElementById('scr').getContext('2d').getImageData(0, 0, 1, 1).data[3] > 0"},
                {
                    "eval": "[FEED.n, FEED.recs, document.getElementById('map').width > 0, document.querySelectorAll('.mod').length > 0]"
                },
                {
                    "eval": "document.querySelectorAll('.stat b').length + ':' + document.querySelector('.stat b').textContent"
                },
            ],
        )
        self.assertEqual(d["results"][0], "4 functions lit by this run")
        self.assertTrue(d["results"][1])
        n, recs, canvas, mods = d["results"][2]
        self.assertEqual((n, canvas, mods), (4, True, True))
        self.assertGreaterEqual(recs, 2)
        self.assertEqual(d["results"][3], "4:46.4%")

    def test_function_view_is_one_fetch_with_c_hops_and_cases(self):
        s = self.stub()
        d = self.drive(
            s,
            "#/f/Menu_Draw",
            [
                {"wait": 1800},
                {"eval": "document.querySelector('.fnh h2').textContent"},
                {"eval": "[...document.querySelectorAll('.hop')].map(h => h.textContent)"},
                {
                    "eval": "document.querySelector('.code').textContent.includes('Engine_Tick();') && document.querySelectorAll('.code tr.m').length"
                },
                {"eval": "document.querySelector('.cases').textContent"},
                {"eval": "[...document.querySelectorAll('.movies .chip')].map(c => c.textContent)"},
            ],
        )
        r = d["results"]
        self.assertEqual(r[0], "Menu_Draw")
        self.assertEqual(r[1], ["Engine_Tick", "Gfx_Init"])
        self.assertEqual(r[2], 1)
        self.assertIn("Menu_Draw(a0=1) -> v0=0", r[3])
        self.assertEqual(r[4], ["boot-idle"])
        self.assertEqual(s.gets.count("/data/fn/Menu_Draw.json"), 1)
        self.assertTrue(s.gets.count("/video/r-live") >= 1)  # the stream plays while the code is open

    def test_search_finds_functions_and_movies_and_opens_a_function(self):
        s = self.stub()
        d = self.drive(
            s,
            "#/w/r-live",
            [
                {"wait": 1500},
                {
                    "eval": "(() => { const q = document.getElementById('q'); q.value = 'menu'; q.dispatchEvent(new Event('input')); return [...document.querySelectorAll('.res .nm')].map(e => e.textContent); })()"
                },
                {
                    "eval": "(() => { document.getElementById('q').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); return 1; })()"
                },
                {"wait": 600},
                {"eval": "[UI.fn, document.querySelector('.fnh h2') && document.querySelector('.fnh h2').textContent]"},
            ],
        )
        names = d["results"][0]
        self.assertIn("Menu_Draw", names)
        self.assertIn("menu-crash", names)
        self.assertEqual(d["results"][2], ["Menu_Draw", "Menu_Draw"])

    def test_grid_hover_card_and_touch_peek(self):
        s = self.stub()
        probe = "(() => { const L = MZ.layout, i = D.fnIndex.get('Gfx_Init'), p = L.pos.get(i), b = document.getElementById('map').getBoundingClientRect(); return [b.left + p[0] + 2, b.top + p[1] + 2]; })()"
        d = self.drive(s, "#/w/r-done", [{"wait": 1500}, {"eval": probe}])
        x, y = d["results"][0]
        d = self.drive(
            s,
            "#/w/r-done",
            [
                {"wait": 1500},
                {"move": [x, y]},
                {"wait": 500},
                {"eval": "[document.getElementById('hc').hidden, document.getElementById('hc').textContent]"},
            ],
        )
        hidden, text = d["results"][0]
        self.assertFalse(hidden)
        self.assertIn("Gfx_Init", text)
        self.assertIn("port + original", text)

    def test_control_takes_drives_and_releases(self):
        s = self.stub()
        self.drive(
            s,
            "#/w/r-live",
            [
                {"wait": 1500},
                {"eval": "document.getElementById('ctl').click()"},
                {"wait": 400},
                {
                    "eval": "document.dispatchEvent(new KeyboardEvent('keydown', { code: 'KeyX', key: 'x', bubbles: true }))"
                },
                {"wait": 300},
                {
                    "eval": "document.dispatchEvent(new KeyboardEvent('keyup', { code: 'KeyX', key: 'x', bubbles: true }))"
                },
                {"wait": 300},
                {"eval": "document.getElementById('ctl').click()"},
                {"wait": 600},
            ],
        )
        paths = [p for p, _ in s.posts]
        self.assertEqual(paths[0], "/api/take")
        inputs = [b for p, b in s.posts if p == "/api/input" and not b.get("release")]
        self.assertTrue(any(b["buttons"] == 0x8000 for b in inputs), inputs)
        self.assertTrue(any(b["buttons"] == 0 for b in inputs[1:]))
        self.assertEqual(s.posts[-1], ("/api/input", {"id": "r-live", "release": True}))
        self.assertNotIn("/api/stop", paths)

    def test_control_falls_back_to_stop_when_the_server_cannot_release(self):
        s = self.stub(release=False)
        self.drive(
            s,
            "#/w/r-live",
            [
                {"wait": 1500},
                {"eval": "document.getElementById('ctl').click()"},
                {"wait": 400},
                {"eval": "document.getElementById('ctl').click()"},
                {"wait": 600},
            ],
        )
        driven = [
            p for p, b in s.posts if not (p == "/api/input" and "buttons" in b)
        ]  # pad heartbeats between take and release are not the contract
        self.assertEqual(driven, ["/api/take", "/api/input", "/api/stop"])

    def test_control_with_nothing_running_starts_a_human_run(self):
        s = self.stub()
        self.drive(s, "#/w/r-done", [{"wait": 1500}, {"eval": "document.getElementById('ctl').click()"}, {"wait": 600}])
        self.assertEqual(s.posts[0][0], "/api/play")
        self.assertEqual(s.posts[0][1].get("label"), "owner")
        self.assertNotIn("movie", s.posts[0][1])

    def test_movie_tile_starts_a_turbo_run(self):
        s = self.stub()
        self.drive(
            s,
            "",
            [
                {"wait": 1500},
                {"eval": "document.querySelector('.tile[data-movie=\"menu-crash\"]').click()"},
                {"wait": 500},
            ],
        )
        path, body = s.posts[0]
        self.assertEqual(path, "/api/play")
        self.assertEqual((body["movie"], body["speed"]), ("menu-crash", 0))

    def test_side_grid_is_on_every_route(self):
        s = self.stub()
        for size in ((1440, 900), (390, 844)):
            for h in ("", "#/w/r-live", "#/f/Menu_Draw", "#/film"):
                d = self.drive(
                    s,
                    h,
                    [
                        {"wait": 1500},
                        {
                            "eval": "(() => { const m = document.getElementById('map'), b = m && m.getBoundingClientRect(); return !!b && b.width > 0 && b.height > 0 && b.top < innerHeight && document.querySelectorAll('#side').length === 1; })()"
                        },
                    ],
                    size=size,
                )
                self.assertTrue(d["results"][0], (h, size))

    def test_home_grid_lights_the_union_of_live_runs_from_the_wall(self):
        s = self.stub()
        d = self.drive(
            s,
            "",
            [
                {"wait": 2000},
                {"eval": "document.getElementById('feedtxt').textContent"},
                {"eval": "[reachers(0), reachers(1), document.querySelector('[data-lit]').textContent]"},
                {
                    "eval": "(() => { UN.tile = 'r-live'; drawMaze(); feedText(); return document.getElementById('feedtxt').textContent; })()"
                },
                {
                    "eval": "fnCard(3).includes('no live run') ? 'none' : fnCard(3).includes('rdot') ? 'named' : 'missing'"
                },
            ],
        )
        self.assertEqual(d["results"][0], "2 functions lit by 1 live runs")
        self.assertEqual(d["results"][1], [["r-live"], [], "2"])
        self.assertTrue(d["results"][2].startswith("2 functions lit by "), d["results"][2])
        self.assertEqual(d["results"][3], "named")

    def test_lockstep_shows_both_sides_on_one_poll_with_one_scrubber_and_diff(self):
        s = self.stub()
        d = self.drive(
            s,
            "#/w/r-done",
            [
                {"wait": 1200},
                {"eval": "action('split', watchedRun()), 1"},
                {"wait": 800},
                {
                    "eval": "[LS.rows.length, LS.i, document.getElementById('lsp').textContent, document.getElementById('lso').textContent, document.getElementById('lsd').textContent]"
                },
                {"eval": "lockstepStep(-1), 1"},
                {"wait": 400},
                {
                    "eval": "[LS.i, document.getElementById('lsp').textContent, document.getElementById('lso').textContent]"
                },
                {"eval": "lockstepPick(190), 1"},
                {"wait": 400},
                {"eval": "action('diff', watchedRun()), 1"},
                {"wait": 800},
                {
                    "eval": "[LS.i, LS.diff, LS.share, document.querySelector('[data-act=diff]').classList.contains('on')]"
                },
            ],
        )
        self.assertEqual(d["results"][1], [2, 1, "port · poll 200", "original · poll 200", "2/2 · diff 25.0%"])
        self.assertEqual(d["results"][3], [0, "port · poll 100", "original · poll 100"])
        self.assertEqual(d["results"][6], [1, True, 0, True])  # the same picture on both sides differs nowhere
        self.assertIn("/diff/r-done", s.gets)
        self.assertNotIn("/oracle/r-done", s.gets)

    def test_phone_layout_has_no_horizontal_scroll(self):
        s = self.stub()
        for h in ("", "#/w/r-live", "#/f/Menu_Draw", "#/w/r-done"):
            d = self.drive(
                s, h, [{"wait": 1800}, {"eval": "[document.documentElement.scrollWidth, innerWidth]"}], size=(390, 844)
            )
            sw, iw = d["results"][0]
            self.assertLessEqual(sw, iw, h)

    def test_grid_redraw_is_fast(self):
        s = self.stub()
        d = self.drive(
            s,
            "#/w/r-done",
            [
                {"wait": 1500},
                {
                    "eval": "(() => { const t = performance.now(); for (let i = 0; i < 20; i++) drawMaze(); return (performance.now() - t) / 20; })()"
                },
            ],
        )
        self.assertLess(d["results"][0], 4)


if __name__ == "__main__":
    unittest.main()
