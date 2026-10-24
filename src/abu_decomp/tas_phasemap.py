"""The TAS page's phase map backend: /api/phase, /api/phase/map?version=V and /phase/frame.png (contract:
orch plans/TAS-PAGE-CONTRACT.md, "Phase map and video wall").

The map is one cell per `cell_bytes` ROM bytes of the version's code and data segments (the config.toml segments
  with a
`vram`, each ending where the next segment starts, concatenated in ROM order), laid out on a Hilbert curve of order k
(smallest with 4^k cells >= bytes / cell_bytes). Segment ranges are fixed by the cartridge, so a cell never moves;
  only its
colour changes. A cell's state is the state of the row covering most of its bytes:

  0 unassigned, 1 data / blob / fill, 2 unmapped function (no C), 3 fuzzy, 4 exact, 5 FAKEMATCH, 6 clean-named,
  7 covered (the port ran it; the high nibble is gcov line % / 10).

Sources, all read without a checkout: `config.toml`, `docs/map-<version>.json` and the FAKEMATCH comments at the
  serve pin
(`tas.serve.pin`, git show / git grep at its HEAD); the coverage table (`<tas.runs>/coverage-table.json`); the seat
profile's optional `phase` section (module phase: `out` = the phase JSON served as is, `named_rule`, `focus`, and
  per game
`versions` + `foundations` TSV template). Without a `phase` section a function is clean-named when it is not
  `func_<8 hex>`.

`frame_png` renders the same cells with the same palette (stdlib zlib writer, byte-identical for identical input);
`Frames` writes one per version to `<tas.runs>/phase/frames/<version>/<STAMP>.png` when the cells changed.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import struct
import subprocess
import threading
import time
import tomllib
import zlib
from pathlib import Path
from typing import Any

CELL_BYTES = 8
STATES = {
    "unassigned": 0,
    "data": 1,
    "unmapped": 2,
    "fuzzy": 3,
    "exact": 4,
    "fakematch": 5,
    "named": 6,
    "covered": 7,
}
STATE_NAMES = {v: k for k, v in STATES.items()}
PALETTE = {
    0: (0x22, 0x22, 0x22),
    1: (0x6B, 0x72, 0x80),
    2: (0x55, 0x55, 0x55),
    3: (0xD9, 0xA4, 0x00),
    4: (0x3B, 0x7D, 0xDD),
    5: (0x3B, 0x7D, 0xDD),
    6: (0x1A, 0xA7, 0xA1),
    7: (0x3F, 0xC3, 0x5A),
}
STRIP_HEIGHT = 32
DEFAULT_NAMED = r"^(func_|D_)[0-9A-F]{8}"
_DEFINITION = re.compile(r"^(?:[A-Za-z_]\w*[\s*]+)+\**([A-Za-z_]\w*)\s*\(")


# --- reading the sources ---


def git_show(repo: Path, ref: str, path: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def segments(config_text: str, version: str) -> list[list[int]]:
    """[[rom_start, rom_end]] of the version's segments that have a vram, in ROM order (a segment ends where the next
    one starts; the last at the cartridge size)."""
    table = tomllib.loads(config_text).get("version", {}).get(version)
    if not table:
        return []
    rows = sorted(table.get("segments", []), key=lambda s: int(s["rom"]))
    out = []
    for index, seg in enumerate(rows):
        if "vram" not in seg:
            continue
        end = int(rows[index + 1]["rom"]) if index + 1 < len(rows) else int(table.get("size", 0))
        if end > int(seg["rom"]):
            out.append([int(seg["rom"]), end])
    return out


def order(total_bytes: int, cell_bytes: int = CELL_BYTES) -> int:
    """Smallest k with 4^k >= bytes / cell_bytes."""
    need = max(1, -(-total_bytes // cell_bytes))
    k = 0
    while 4**k < need:
        k += 1
    return k


def fakematch_fns(repo: Path, ref: str) -> set[str]:
    """Functions whose body carries a FAKEMATCH comment: the nearest definition line above each comment."""
    done = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "FAKEMATCH", ref, "--", "src"],
        capture_output=True,
        text=True,
        check=False,
    )
    found: set[str] = set()
    for entry in done.stdout.split():
        path = entry.partition(":")[2] or entry
        text = git_show(repo, ref, path) or ""
        name = ""
        for line in text.splitlines():
            match = _DEFINITION.match(line)
            if match:
                name = match.group(1)
            if "FAKEMATCH" in line and name:
                found.add(name)
    return found


def foundations(template: str | None, version: str) -> set[str]:
    """Function names of the foundations TSV (category, function, ...); empty when absent."""
    if not template:
        return set()
    try:
        lines = Path(template.format(version=version)).read_text().splitlines()
    except (OSError, KeyError, IndexError):
        return set()
    return {cols[1] for cols in (ln.split("\t") for ln in lines) if len(cols) > 1 and cols[0] != "category"}


# --- the cells ---


def fn_state(row: list[Any], fake: set[str], named: re.Pattern[str], hit: bool) -> int:
    name, score, file = row[0], row[3], row[4] or ""
    if not file or file.endswith(".s"):
        return STATES["unmapped"]
    if score < 100:
        return STATES["fuzzy"]
    if name in fake:
        return STATES["fakematch"]
    if hit:
        return STATES["covered"]
    return STATES["exact"] if named.search(name) else STATES["named"]


def build(
    version: str,
    segs: list[list[int]],
    map_json: dict[str, Any],
    coverage: dict[str, dict[str, Any]],
    fake: set[str],
    foundation: set[str],
    named: re.Pattern[str],
    ref: str,
    generated: Any,
) -> dict[str, Any]:
    """The /api/phase/map body. `coverage` is name -> coverage-table row (port.hit, optional line_percent)."""
    starts, total = [], 0
    for lo, hi in segs:
        starts.append((lo, hi, total))
        total += hi - lo
    k = order(total)
    best_bytes = [0] * (4**k)
    best_state = [0] * (4**k)

    def virtual(lo: int, hi: int) -> list[tuple[int, int]]:
        out = []
        for s_lo, s_hi, base in starts:
            a, b = max(lo, s_lo), min(hi, s_hi)
            if a < b:
                out.append((base + a - s_lo, base + b - s_lo))
        return out

    def paint(v_lo: int, v_hi: int, state: int) -> None:
        for cell in range(v_lo // CELL_BYTES, (v_hi - 1) // CELL_BYTES + 1):
            over = min(v_hi, (cell + 1) * CELL_BYTES) - max(v_lo, cell * CELL_BYTES)
            if over > best_bytes[cell]:
                best_bytes[cell], best_state[cell] = over, state

    for lo, hi in _data_ranges(map_json):
        for v_lo, v_hi in virtual(lo, hi):
            paint(v_lo, v_hi, STATES["data"])
    fns, files = [], {}
    rows = sorted(map_json.get("fns", []), key=lambda r: r[1])
    for row in rows:
        name, rom, size = row[0], row[1], row[2]
        cov = coverage.get(name) or {}
        hit = bool((cov.get("port") or {}).get("hit"))
        state = fn_state(row, fake, named, hit)
        pct = cov.get("line_percent") if state == STATES["covered"] else None
        for v_lo, v_hi in virtual(rom, rom + size):
            value = state | (min(15, round((pct or 0) / 10)) << 4 if state == STATES["covered"] else 0)
            paint(v_lo, v_hi, value)
            first, last = v_lo // CELL_BYTES, (v_hi - 1) // CELL_BYTES
            span = files.setdefault(row[4] or "", [first, last])
            span[0], span[1] = min(span[0], first), max(span[1], last)
        if virtual(rom, rom + size):
            fns.append([name, rom, size, STATE_NAMES[state], pct, row[4] or "", name in foundation])
    cells = bytes(best_state)
    blob_bytes = _outside(segs, [(int(b[1]), int(b[1]) + int(b[2])) for b in map_json.get("blobs", [])])
    return {
        "version": version,
        "segments": segs,
        "cell_bytes": CELL_BYTES,
        "order": k,
        "ref": ref,
        "generated": generated,
        "blobs": {"mapped": blob_bytes, "bytes": max(blob_bytes, int(map_json.get("rom_size", 0)) - total)},
        "cells": base64.b64encode(cells).decode(),
        "fns": fns,
        "files": sorted(([f, a, b] for f, (a, b) in files.items() if f), key=lambda x: (x[1], x[0])),
    }


def _outside(segs: list[list[int]], ranges: list[tuple[int, int]]) -> int:
    """Bytes covered by the union of `ranges` that lie outside every segment."""
    covered, edge = 0, 0
    for lo, hi in sorted(ranges):
        pieces = [(lo, hi)]
        for s_lo, s_hi in segs:
            pieces = [q for a, b in pieces for q in ((a, min(b, s_lo)), (max(a, s_hi), b)) if q[0] < q[1]]
        for a, b in pieces:
            a = max(a, edge)
            if b > a:
                covered, edge = covered + b - a, b
    return covered


def _data_ranges(map_json: dict[str, Any]) -> list[tuple[int, int]]:
    out = []
    for key in ("data", "fill", "platform"):
        for pair in (map_json.get("spans") or {}).get(key, []):
            if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], int):
                out.append((pair[0], pair[1]))
    return sorted(out)


# --- the picture ---


def d2xy(k: int, d: int) -> tuple[int, int]:
    """Hilbert curve index -> (x, y) on a 2^k square (the standard mapping the page also uses)."""
    x = y = 0
    t = d
    s = 1
    while s < (1 << k):
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return x, y


def _shade(state_value: int, x: int, y: int) -> tuple[int, int, int]:
    state = state_value & 15
    r, g, b = PALETTE[state]
    if state == STATES["covered"]:
        scale = 0.55 + 0.45 * min(10, state_value >> 4) / 10 if state_value >> 4 else 1.0
        return int(r * scale), int(g * scale), int(b * scale)
    if state == STATES["fakematch"] and (x + y) % 4 < 2:
        return int(r * 0.6), int(g * 0.6), int(b * 0.6)
    return r, g, b


def png(width: int, height: int, rgb: bytes) -> bytes:
    """A deterministic 8-bit RGB PNG (stdlib zlib, level 9, no ancillary chunks)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def frame_png(body: dict[str, Any], gates: list[dict[str, Any]] | None = None) -> bytes:
    """The map as a 2^k square (one pixel per cell) plus a 32 px gate strip: one equal-width bar per gate row, filled
    to value/threshold, green when met, amber when not."""
    k = body["order"]
    side = 1 << k
    cells = base64.b64decode(body["cells"])
    pixels = bytearray(b"\x22" * (side * (side + STRIP_HEIGHT) * 3))
    for d, value in enumerate(cells):
        x, y = d2xy(k, d)
        at = (y * side + x) * 3
        pixels[at : at + 3] = bytes(_shade(value, x, y))
    rows = gates or []
    for index, gate in enumerate(rows):
        lo, hi = index * side // len(rows), (index + 1) * side // len(rows) - 2
        try:
            share = max(0.0, min(1.0, float(gate["value"]) / float(gate["threshold"])))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            share = 0.0
        colour = bytes((0x3F, 0xC3, 0x5A) if gate.get("met") else (0xD9, 0xA4, 0x00))
        for y in range(side + 4, side + STRIP_HEIGHT - 4):
            for x in range(lo, hi):
                at = (y * side + x) * 3
                pixels[at : at + 3] = colour if x - lo < (hi - lo) * share else b"\x44\x44\x44"
    return png(side, side + STRIP_HEIGHT, bytes(pixels))


# --- the service ---


class Phase:
    """What `tas serve` mounts: reads the profile's `phase` section and the pin lazily, caches the map per
    (version, pin sha, coverage generated)."""

    def __init__(
        self,
        profile: Any,
        cfg: dict[str, Any],
        pin: Path | None,
        table: Any,
        frame_minutes: float = 10,
        given: dict[str, Any] | None = None,
    ) -> None:
        self.cfg, self.pin, self.table, self.frame_minutes = cfg, pin, table, float(frame_minutes)
        section = given
        if section is None:
            try:
                section = json.loads(Path(profile.source_file).read_text()).get("phase") or {}
            except (OSError, ValueError, AttributeError):
                section = {}
        self.section = section if isinstance(section, dict) else {}
        self.out = Path(self.section["out"]) if self.section.get("out") else None
        try:
            self.named = re.compile(str(self.section.get("named_rule") or DEFAULT_NAMED))
        except re.error:
            self.named = re.compile(DEFAULT_NAMED)
        self.lock = threading.Lock()
        self.cache: dict[Any, dict[str, Any]] = {}

    # versions and the focus come from the phase section; the tas.coverage version otherwise
    def versions(self) -> list[str]:
        out: list[str] = []
        for game in (self.section.get("games") or {}).values():
            out += [v for v in game.get("versions", []) if v not in out]
        return out

    def focus(self) -> str | None:
        return (self.section.get("focus") or {}).get("version") or (self.versions() or [None])[0]

    def foundation_template(self, version: str) -> str | None:
        for game in (self.section.get("games") or {}).values():
            if version in game.get("versions", []):
                return game.get("foundations")
        return None

    def phase(self) -> tuple[Any, int]:
        """(body, status): the phase JSON file as written by `decomp phase`, or a 404 error body."""
        if not self.out:
            return {"error": "no phase.out in the profile"}, 404
        try:
            return json.loads(self.out.read_text()), 200
        except (OSError, ValueError):
            return {"error": "phase.out not found (decomp phase has not run)"}, 404

    def sha(self) -> str | None:
        if not self.pin:
            return None
        done = subprocess.run(
            ["git", "-C", str(self.pin), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        )
        return done.stdout.strip() or None

    def etag(self, version: str) -> str | None:
        sha = self.sha()
        return f'"{sha}-{(self.table() or {}).get("generated")}-{version}"' if sha else None

    def map(self, version: str) -> tuple[dict[str, Any], int]:
        sha = self.sha()
        if not sha or not self.pin:
            return {"error": "no serve pin"}, 404
        table = self.table() or {}
        key = (version, sha, table.get("generated"))
        with self.lock:
            hit = self.cache.get(version)
            if hit and hit["key"] == key:
                return hit["body"], 200
        config = git_show(self.pin, sha, "config.toml")
        text = git_show(self.pin, sha, f"docs/map-{version}.json")
        segs = segments(config, version) if config else []
        if not segs or text is None:
            return {"error": f"no map for version {version}"}, 404
        try:
            map_json = json.loads(text)
        except ValueError:
            return {"error": f"unreadable map for version {version}"}, 404
        coverage = {r["name"]: r for r in table.get("functions", []) if r.get("in_version")}
        body = build(
            version,
            segs,
            map_json,
            coverage,
            fakematch_fns(self.pin, sha),
            foundations(self.foundation_template(version), version),
            self.named,
            sha,
            table.get("generated"),
        )
        with self.lock:
            self.cache[version] = {"key": key, "body": body}
        return body, 200

    def gates(self, version: str) -> list[dict[str, Any]]:
        body, status = self.phase()
        rows = body.get("rows", []) if status == 200 and isinstance(body, dict) else []
        return [r for r in rows if r.get("version") in (version, None)]

    def frame(self, version: str) -> tuple[bytes | None, str]:
        body, status = self.map(version)
        return (frame_png(body, self.gates(version)), "") if status == 200 else (None, body.get("error", ""))

    # --- the timelapse frames ---

    def write_frames(self, now: float | None = None) -> list[Path]:
        """One PNG per version whose cells changed since the last frame; returns the files written."""
        written = []
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
        for version in self.versions() or [v for v in [self.focus()] if v]:
            body, status = self.map(version)
            if status != 200:
                continue
            folder = self.cfg["runs"] / "phase" / "frames" / version
            digest = hashlib.sha1(body["cells"].encode()).hexdigest()
            last = folder / ".cells"
            try:
                if last.read_text().strip() == digest:
                    continue
            except OSError:
                pass
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{stamp}.png"
            path.write_bytes(frame_png(body, self.gates(version)))
            last.write_text(digest + "\n")
            written.append(path)
        return written

    def run_frames(self) -> None:
        """Thread body: write frames every `frame_minutes` (a failed pass waits for the next one)."""
        while True:
            with contextlib.suppress(Exception):  # the page must outlive a bad map
                self.write_frames()
            time.sleep(max(1.0, self.frame_minutes * 60))
