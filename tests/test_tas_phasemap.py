"""The phase map backend (module tas_phasemap): fixed cells, PNG bytes, routes."""

import base64
import itertools
import json
import re
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

from abu_decomp import tas_phasemap, tas_serve
from abu_decomp.config import load_profile

CONFIG = """
[version.t]
size = 0x400
segments = [
  { name = "retained_0", rom = 0x0, kind = "bin" },
  { name = "code", rom = 0x100, vram = 0x80000000 },
  { name = "retained_200", rom = 0x200, kind = "bin" },
  { name = "data", rom = 0x300, vram = 0x80100000 },
  { name = "retained_340", rom = 0x340, kind = "bin" },
]
"""
FNS = [
    ["Fuzz", 0x100, 0x40, 50.0, "src/a.c", 1],
    ["Covered", 0x140, 0x40, 100.0, "src/a.c", 2],
    ["FakeFn", 0x180, 0x20, 100.0, "src/b.c", 3],
    ["func_80001234", 0x1A0, 0x20, 100.0, "src/b.c", 4],
    ["NoC", 0x1C0, 0x20, 0.0, "", 5],
    ["Named", 0x1E0, 0x20, 100.0, "src/b.c", 6],
]
MAP = {
    "version": "t",
    "rom_size": 0x400,
    "fns": FNS,
    "blobs": [["x", 0x200, 0x80, "d", "g"]],
    "spans": {"data": [[0x300, 0x340]]},
}
# virtual cells (8 B each): 0-7 fuzzy, 8-15 covered at 80 % (7 | 8 << 4), 16-19 fakematch, 20-23 exact, 24-27 unmapped,
# 28-31 named, 32-39 data, 40-63 unassigned
EXPECTED = bytes([3] * 8 + [0x87] * 8 + [5] * 4 + [4] * 4 + [2] * 4 + [6] * 4 + [1] * 8 + [0] * 24)
COVERAGE = {"Covered": {"in_version": True, "name": "Covered", "port": {"hit": True}, "line_percent": 80.0}}


class Cells(unittest.TestCase):
    def build(self):
        named = re.compile(tas_phasemap.DEFAULT_NAMED)
        segs = tas_phasemap.segments(CONFIG, "t")
        return segs, tas_phasemap.build("t", segs, MAP, COVERAGE, {"FakeFn"}, {"Fuzz", "Named"}, named, "abc", 7)

    def test_segments_are_the_vram_ones_ending_at_the_next_start(self):
        segs, body = self.build()
        self.assertEqual(segs, [[0x100, 0x200], [0x300, 0x340]])
        self.assertEqual((body["order"], body["cell_bytes"]), (3, 8))
        self.assertEqual(body["blobs"], {"mapped": 0x80, "bytes": 0x400 - 0x140})

    def test_order_is_the_smallest_k_with_enough_cells(self):
        self.assertEqual([tas_phasemap.order(n) for n in (8, 9, 128, 129, 1_400_000)], [0, 1, 2, 3, 9])

    def test_cells_are_fixed(self):
        _, body = self.build()
        self.assertEqual(base64.b64decode(body["cells"]), EXPECTED)
        self.assertEqual(body["cells"], base64.b64encode(EXPECTED).decode())

    def test_rows_for_hover(self):
        _, body = self.build()
        by = {r[0]: r for r in body["fns"]}
        self.assertEqual(by["Covered"], ["Covered", 0x140, 0x40, "covered", 80.0, "src/a.c", False])
        self.assertEqual(by["FakeFn"][3], "fakematch")
        self.assertEqual(by["func_80001234"][3], "exact")
        self.assertEqual(by["NoC"][3], "unmapped")
        self.assertTrue(by["Fuzz"][6] and by["Named"][6])
        self.assertEqual(body["files"], [["src/a.c", 0, 15], ["src/b.c", 16, 31]])

    def test_hilbert_visits_every_cell_once_with_adjacent_steps(self):
        pts = [tas_phasemap.d2xy(3, d) for d in range(64)]
        self.assertEqual(len(set(pts)), 64)
        self.assertTrue(all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in itertools.pairwise(pts)))

    def test_png_is_deterministic_and_valid(self):
        _, body = self.build()
        gates = [{"value": 50, "threshold": 100, "met": False}, {"value": 100, "threshold": 90, "met": True}]
        one, two = tas_phasemap.frame_png(body, gates), tas_phasemap.frame_png(body, gates)
        self.assertEqual(one, two)
        self.assertEqual(one[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", one[16:24])
        self.assertEqual((width, height), (8, 8 + tas_phasemap.STRIP_HEIGHT))
        idat = one[one.index(b"IDAT") + 4 : one.index(b"IEND") - 8]
        self.assertEqual(len(zlib.decompress(idat)), height * (1 + width * 3))
        other = dict(body, cells=base64.b64encode(bytes(64)).decode())
        self.assertNotEqual(tas_phasemap.frame_png(other, gates), one)


class Routes(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.pin = root / "pin"
        (self.pin / "docs").mkdir(parents=True)
        (self.pin / "src").mkdir()
        (self.pin / "config.toml").write_text(CONFIG)
        (self.pin / "docs" / "map-t.json").write_text(json.dumps(MAP))
        (self.pin / "src" / "b.c").write_text("int FakeFn(int a)\n{\n    /* FAKEMATCH */\n    return a;\n}\n")
        git = ["git", "-C", str(self.pin), "-c", "user.name=t", "-c", "user.email=t@t"]
        for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
            subprocess.run([*git, *args], check=True, capture_output=True)
        (root / "runs").mkdir()
        (root / "movies").mkdir()
        (root / "game.z64").write_bytes(b"rom")
        (root / "port").write_text("bin")
        (root / "runs" / "coverage-table.json").write_text(
            json.dumps({"generated": 5, "functions": list(COVERAGE.values())})
        )
        self.out = root / "phase.json"
        self.root = root
        seat = root / "seat.json"
        seat.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "t",
                    "tas": {
                        "movies": "movies", "runs": "runs", "port": "port", "rom": "game.z64", "args": [],
                        "serve": {"listen": "127.0.0.1:0", "pin": "pin", "phase_frame_minutes": 1},
                    },
                }
            )
        )  # fmt: skip
        self.server = tas_serve.Server(load_profile(seat))
        section = {"out": str(self.out), "focus": {"version": "t"}, "games": {"G": {"versions": ["t"]}}}
        self.server.phase = tas_phasemap.Phase(
            None, self.server.cfg, self.server.pin_dir, self.server.table, 1, given=section
        )
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), tas_serve.handler(self.server))
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def get(self, path, **headers):
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.headers, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_phase_is_404_json_until_phase_out_exists_then_served_as_is(self):
        status, _, body = self.get("/api/phase")
        self.assertEqual((status, list(json.loads(body))), (404, ["error"]))
        self.out.write_text('{"generated": 1, "rows": []}')
        status, _, body = self.get("/api/phase")
        got = json.loads(body)
        self.assertEqual((status, got["generated"], got["rows"], got["stage"]), (200, 1, [], None))

    def test_phase_rows_get_numeric_phase_top_level_stage_and_a_shot_link(self):
        evidence = self.root / "evidence" / "linux"
        evidence.mkdir(parents=True)
        (evidence / "evidence.json").write_text(json.dumps({"screenshot": "title.png"}))
        (evidence / "title.png").write_bytes(b"\x89PNG")
        (self.root / "secret.png").write_bytes(b"no")
        (self.root / "evidence" / "windows").mkdir()
        (self.root / "evidence" / "windows" / "evidence.json").write_text(
            json.dumps({"screenshot": "../../secret.png"})
        )
        self.server.phase.section["platforms"] = {
            "linux": str(evidence / "evidence.json"),
            "windows": str(self.root / "evidence" / "windows" / "evidence.json"),
        }
        self.out.write_text(
            json.dumps(
                {
                    "rows": [
                        {"phase": "p1", "key": "mapped_pct"},
                        {"phase": "p4", "key": "linux.r0"},
                        {"phase": "p4", "key": "windows.r0"},
                    ],
                    "detail": {"p4": {"stage": None, "next_stage": None, "unknown": "coverage report absent"}},
                }
            )
        )
        got = json.loads(self.get("/api/phase")[2])
        self.assertEqual([r["phase"] for r in got["rows"]], [1, 4, 4])
        self.assertEqual(got["rows"][1]["shot"], "/phase/shot?platform=linux")
        self.assertNotIn("shot", got["rows"][2])  # the screenshot path leaves the evidence folder
        self.assertEqual((got["stage"], got["stage_unknown"]), (None, "coverage report absent"))
        status, headers, body = self.get("/phase/shot?platform=linux")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", b"\x89PNG"))
        self.assertEqual(self.get("/phase/shot?platform=windows")[0], 404)
        self.assertEqual(self.get("/phase/shot?platform=nope")[0], 404)

    def test_map_has_an_etag_and_answers_304(self):
        status, headers, body = self.get("/api/phase/map?version=t")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(base64.b64decode(data["cells"]), EXPECTED)
        self.assertEqual(data["files"][0], ["src/a.c", 0, 15])
        self.assertEqual(self.get("/api/phase/map?version=t", **{"If-None-Match": headers["ETag"]})[0], 304)
        self.assertEqual(self.get("/api/phase/map?version=nope")[0], 404)
        self.assertEqual(json.loads(self.get("/api/phase/map")[2])["version"], "t")  # the focus

    def test_frame_png_is_identical_twice_and_frames_are_written_once(self):
        first, second = self.get("/phase/frame.png?version=t"), self.get("/phase/frame.png?version=t")
        self.assertEqual((first[0], first[1]["Content-Type"]), (200, "image/png"))
        self.assertEqual(first[2], second[2])
        written = self.server.phase.write_frames(now=100.0)
        self.assertEqual([p.name for p in written], ["19700101T000140Z.png"])
        self.assertEqual(written[0].read_bytes(), first[2])
        self.assertEqual(self.server.phase.write_frames(now=700.0), [])  # cells unchanged


if __name__ == "__main__":
    unittest.main()
