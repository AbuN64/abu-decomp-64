"""`decomp tas serve`: the Runner page's server (orch plans/GAME-RUNNER.md, sections 7 and 10a).
Section `tas.serve`: {"listen": "HOST:PORT", "jobs": N, "inbox": path, "states": path, "pin": path, "keep_days": days,
"phase_frame_minutes": m, "port_source": path}; `lanes`, `invocations` and `claims` are still accepted (the bake reads
the last two). Every run (started here, by `tas play` in a shell, or by `play --all`) is a folder under `runs/plays`
whose `run.json` the port's run writes (module tas); the page sees all of them: the port's own latest picture
(`view.bmp`), its log and its sound (`audio.pcm`, served as a growing WAV).
The page is static files: `/data/*` serves `<tas.runs>/site/` (module tas_bake writes it; a `.gz` sibling goes out to
a client that accepts gzip, hashed names are immutable). The one file serve writes there is `live.json`: the running,
queued and newest finished runs, rewritten from an inotify watch on the run folders (class Live), never per request.
`/video/RUN` is the run's sampled JPEG stream; `/wall` one multiplexed stream of 192 px thumbs of every running run;
the live reach stream (class Reach, GAME-RUNNER 10a) arrives from the port as datagrams on `<input socket>.reach`, is
kept as a cumulative bitset, written to RUN/reach.bin at run end, and rides in-band on `/video` (a record whose length
field has REACH_FLAG set; a new viewer gets one full record first).
Any running run can be taken over by live input (`/api/take`, `/api/input`: datagrams to the run's input socket,
tas.input_address), `/api/play` without a movie starts a human run, and `/api/play` with `from` + `at` retries a run
from a poll (tas.play `source`/`take_at`); one run is driven by live input at a time. `/api/pin` keeps a run; every
hour runs older than `keep_days` (default 3) that are neither pinned nor their movie's newest are deleted
(tas.archive). `inbox` is the integration train's inbox, `states` the decomp progress states, `pin` the git folder of
the decomp source the port was built from. Serve gates the regression corpus (module tas_corpus) whenever the port
binary is a build it has not been gated on and nothing runs. Function state (exact / fuzzy / none) is read from the
decomp source, as the progress states define it. A run played with the oracle (module tas_oracle) also shows the
original game: its latest picture (/oracle/<id>), both sides' pictures at each compared poll (/shot/<id>) and the
scores (/diff/<id>).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image

from abu_decomp import (
    DecompError,
    evidence,
    inotify,
    tas,
    tas_board,
    tas_corpus,
    tas_coverage,
    tas_health,
    tas_oracle,
    tas_phasemap,
    tas_points,
    tas_triage,
)
from abu_decomp.config import Profile

_SERVE_KEYS = frozenset(
    {
        "listen",
        "jobs",
        "inbox",
        "states",
        "pin",
        "lanes",
        "keep_days",
        "invocations",
        "claims",
        "phase_frame_minutes",
        "port_source",
    }
)
RECENT_SHOWN = 24  # newest finished runs in live.json (all running and queued ones are always there)
HISTORY_PAGE = 60  # runs per /api/runs page
CACHE_SECONDS = 30.0
GZIP_MIN = 1024  # smaller bodies go out as they are
GZIP_KEPT = 16  # compressed bodies kept by tag, so a repeat fetch of an unchanged body costs no compression
TEXT_KINDS = ("application/json", "text/html", "text/css", "text/javascript", "image/svg+xml")
ARCHIVE_SECONDS = 3600.0  # how often old runs are archived
GATE_CHECK_SECONDS = 60.0  # how often serve looks for a port build the regression corpus has not been gated on
STOP_GRACE = 3.0  # seconds a stopped run gets before its process group is killed
VIDEO_FPS = 30  # latest frame sampled at wall pace; no backlog or replay of missed pictures
VIDEO_HEADER = struct.Struct(">IQI")  # JPEG length, capture stamp (microseconds), input sequence
AUDIO_LEAD_SECONDS = 0.25  # a listener joins this far behind the live end of the sound
PAGE = Path(__file__).with_name("runner.html").read_text  # the page file
STATIC = Path(__file__).with_name("static")  # files of the page (uFuzzy, the icon sprite), served at /static/NAME
REACH_FLAG = 0x80000000  # set in a /video header's length field: the body is a reach record, not a JPEG
WALL_TABLE = REACH_FLAG  # set in a /wall header's length field: the body is JSON {"id": run id} naming run index `seq`
WALL_KEEPALIVE_INDEX = 0xFFFFFFFF  # a flagged, empty /wall record: nothing to show, but a closed client is noticed
WALL_FPS = 4
WALL_WIDTH = 192
WALL_KEEPALIVE_SECONDS = 10.0
LIVE_SECONDS = 0.5  # while a run goes, live.json is refreshed at least this often (poll ticks)
LIVE_GAP_SECONDS = 0.2  # live.json is rewritten at most this often: a change shows well inside 0.5 s
LIVE_IDLE_SECONDS = 60.0  # with nothing running, live.json is refreshed this often (its clock, an abandoned run)
REACH_READ_TIMEOUT = 1.0  # how long the reader waits for a datagram before it asks whether the run is still alive
REACH_FILE = "reach.bin"  # RUN/reach.bin: the run's final reach bitset over fns.json rows
REACH_MAX_RECORD = 65536
REACH_MAX_ROW = 1 << 17
REACH_KEPT_RECORDS = 2048  # records a slow /video client may lag behind before it is given a fresh full record
REACH_KEPT_BYTES = 1 << 20
REACH_RUNS_KEPT = 64  # finished runs whose relay stays in memory
HASHED_NAME = re.compile(r"\.[0-9a-f]{6,}\.[a-z]+$")  # fns.1a2b3c4d.json: the content hash is in the name
NO_STORE_DATA = frozenset({"live.json", "manifest.json"})
DATA_KINDS = {
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".bin": "application/octet-stream",
    ".jpg": "image/jpeg",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "text/javascript",
}


def wav_header(rate: int) -> bytes:
    """A RIFF/WAVE header for s16 stereo of unknown length (sizes at their maximum), for a live stream."""
    size = 0xFFFFFFFF
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    return (
        b"RIFF"
        + struct.pack("<I", size)
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", size - 36)
    )


RELEASED = "released"  # in a taken run's folder once given back: the run stays a take, live input drives it no more


def driven(run: Path) -> bool:
    """Whether live input drives the run now: taken and not given back since."""
    return (run / tas.TAKEN).exists() and not (run / RELEASED).exists()


def realtime(run: Path, record: dict[str, Any]) -> bool:
    """The actual pacing, including a turbo prefix and a live takeover, shared by state and audio."""
    if record.get("speed") == 1:
        return True
    # TAKEN is written before a retry's prefix starts. Only the port's acknowledgement means 1x.
    if not driven(run):
        return False
    try:
        return "live input took over at poll " in (run / "output.txt").read_text(errors="replace")
    except OSError:
        return False


def picture(data: bytes, kind: str = "PNG") -> tuple[bytes, int]:
    """One capture decoder for live BMP, archived BMP and PPM shots; PNG stills, JPEG video, THUMB wall tiles.

    Pillow owns offset, signed height, padded stride and channel conversion. Validate the capture contract
    before decoding, preserving the shim's reserved input acknowledgement independently of the pixel bytes.
    """
    seq = 0
    if data.startswith(b"BM") and len(data) >= 54:
        offset, dib = struct.unpack_from("<II", data, 10)
        width, signed_height, planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
        height = abs(signed_height)
        stride = ((width * bits + 31) // 32) * 4
        if dib < 40 or offset < 14 + dib or planes != 1 or bits not in (24, 32) or compression:
            raise ValueError("unsupported BMP layout")
        if not 0 < width <= 4096 or not 0 < height <= 4096 or len(data) < offset + stride * height:
            raise ValueError("incomplete BMP")
        seq = struct.unpack_from("<I", data, 6)[0]
    elif not (data.startswith(tas_oracle.PPM_HEAD) and len(data) == tas_oracle.PPM_SIZE):
        raise ValueError("unsupported capture")
    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        out = io.BytesIO()
        if kind == "THUMB":  # the wall's tile: WALL_WIDTH px wide
            size = (WALL_WIDTH, max(1, round(rgb.height * WALL_WIDTH / rgb.width)))
            rgb.resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0).save(out, "JPEG", quality=70)
        elif kind == "JPEG":
            rgb.save(out, "JPEG", quality=82, subsampling=0)
        else:
            rgb.save(out, "PNG", compress_level=1)
        return out.getvalue(), seq


class Server:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.pictures: OrderedDict[tuple[Path, str], tuple[int, int, bytes, int]] = OrderedDict()
        self.picture_lock = threading.Lock()
        self.cfg = tas.settings(profile)
        serve = dict(profile.section("tas").get("serve", {}))
        extra = serve.keys() - _SERVE_KEYS
        if extra:
            raise DecompError("tas.serve", f"unknown tas.serve keys: {', '.join(sorted(extra))}")
        self.listen = str(serve.get("listen", "127.0.0.1:8099"))
        self.jobs = int(serve.get("jobs", 4))
        self.inbox = profile.path("tas.serve.inbox", serve["inbox"]) if "inbox" in serve else None
        self.states = profile.path("tas.serve.states", serve["states"]) if "states" in serve else None
        self.pin_dir = profile.path("tas.serve.pin", serve["pin"]) if "pin" in serve else None
        self.keep_days = float(serve.get("keep_days", 3))
        self.port_source = (
            profile.path("tas.serve.port_source", serve["port_source"]) if "port_source" in serve else None
        )
        self.phase = tas_phasemap.Phase(
            profile, self.cfg, self.pin_dir, self.table, float(serve.get("phase_frame_minutes", 10))
        )
        self.oracles: dict[str, tuple[int, Any]] = {}  # run id -> (record mtime, summary) of finished runs
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.jobs))
        self.lock = threading.Lock()
        self.queued: dict[str, dict[str, Any]] = {}  # id -> placeholder row until the run writes its record
        self.cache: dict[Any, tuple[float, Any]] = {}
        self.gating = False
        self.gz: dict[str, bytes] = {}
        self.site = self.cfg["runs"] / "site"  # the baked site (module tas_bake) and live.json
        self.live = Live(self)
        self.reach = Reach()

    # --- cached facts ---

    def _cached(self, key: Any, make: Any, seconds: float = CACHE_SECONDS) -> Any:
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = make()
        self.cache[key] = (time.monotonic(), value)
        return value

    def gzipped(self, tag: str, body: bytes) -> bytes:
        """`body` compressed, kept under its tag (the newest few) so an unchanged body is compressed once."""
        with self.lock:
            hit = self.gz.get(tag)
        if hit is None:
            packer = zlib.compressobj(5, zlib.DEFLATED, 31)
            hit = packer.compress(body) + packer.flush()
            with self.lock:
                self.gz[tag] = hit
                while len(self.gz) > GZIP_KEPT:
                    del self.gz[next(iter(self.gz))]
        return hit

    def pin_sha(self) -> str | None:
        def read() -> str | None:
            if not self.pin_dir:
                return None
            out = subprocess.run(
                ["git", "-C", str(self.pin_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
            )
            return out.stdout.strip() or None

        return self._cached("pin", read)

    def fn_state(self, name: str) -> str:
        """exact / fuzzy / none for one function, as the progress states define them."""

        def read() -> str:
            try:
                none = json.loads(self.states.read_text()) if self.states else {}
            except (OSError, ValueError):
                none = {}
            for version in (none.get("versions") or {}).values():
                if name in tas_board.none_names(version):
                    return "none"
            project = tas_triage._project(self.profile)
            site = tas_triage.definition_site(project, name) if project else None
            if site and project:
                fn = evidence.parse((project / site.rpartition(":")[0]).read_text(errors="replace")).get(name)
                return "fuzzy" if fn is not None and fn.kind == evidence.NM else "exact"
            return "none"

        return self._cached(("fn", name), read)

    def movie_meta(self, path: Path) -> dict[str, Any]:
        """A movie's poll count and branch: `root` (the movie its chain of takes started from), `from` / `at` (the
        movie and poll a take left, from its header)."""

        def read() -> dict[str, Any]:
            try:
                header, polls = tas.decode(path.read_text())
            except (OSError, DecompError, ValueError):
                header, polls = {}, []
            at = header.get("at", "")
            return {
                "polls": len(polls),
                "root": tas.take_root(path.stem),
                "from": header.get("from") or None,
                "at": int(at) if at.isdigit() else None,
            }

        return self._cached(
            ("movie", str(path), path.stat().st_mtime_ns), read, float("inf")
        )  # the key holds the mtime

    def slot_pool(self) -> dict[str, Any]:
        """The host slot pool (module slots): who holds which slots, who waits, and the last landed->live seconds
        (page-port.py's landings.jsonl beside the runs folder)."""
        from abu_decomp import slots

        out = slots.status()
        rows = []
        try:
            lines = (Path(self.cfg["runs"]).parent / "landings.jsonl").read_text().splitlines()[-20:]
            rows = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError, KeyError):
            pass
        out["landings"] = rows[-5:]
        return out

    def coverage(self) -> dict[str, Any]:
        """The game-code coverage table (runs/coverage.json, written by `tas play --all` / `tas coverage`): the summary
        and the two lists that drive work. {} when no table exists yet."""
        path = self.cfg["runs"] / tas_coverage.TABLE

        def read() -> dict[str, Any]:
            report = tas_coverage.read_table(self.cfg)
            return tas_coverage.lists(report) if report else {}

        try:
            return self._cached(("coverage", path.stat().st_mtime_ns), read)
        except OSError:
            return {}

    def table(self) -> dict[str, Any]:
        """The raw coverage table ({} before the first one), cached by its mtime."""
        path = self.cfg["runs"] / tas_coverage.TABLE
        try:
            return self._cached(("table", path.stat().st_mtime_ns), lambda: tas_coverage.read_table(self.cfg) or {})
        except OSError:
            return {}

    def gatekeeper(self) -> None:
        """Gate the corpus whenever the port binary is a build it was not gated on (page-port.py rebuilds it after each
        train round, a repin, a hand build): only with no run live and the build not older than its pins."""
        while True:
            time.sleep(GATE_CHECK_SECONDS)
            try:
                if not tas_corpus.gate_due(self.cfg):
                    continue
                if any(row.get("status") == "running" for _, row in tas.run_rows(self.cfg)) or self.queued:
                    continue
                if tas_health.read(self.cfg, self.profile).get("pins_stale"):
                    continue
                self.gating = True
                result, _ = tas_corpus.gate(self.profile, jobs=self.jobs)
                print(
                    f"tas page: corpus gate {result['verdict']} {result['counts']} "
                    f"{result['wall_seconds']}/{result['cpu_seconds']} s (wall/cpu)",
                    flush=True,
                )
            except Exception as error:  # the next check tries again; the page shows the last gate file
                print(f"tas page: corpus gate failed to run: {error}", flush=True)
            finally:
                self.gating = False

    # --- the contract ---

    def capture(self, path: Path, kind: str = "PNG") -> tuple[int, bytes, int]:
        """Cache only the newest capture per path, bounded across viewers and archived runs."""
        with self.picture_lock:
            info = path.stat()
            key = (path, kind)
            hit = self.pictures.get(key)
            if hit and hit[:2] == (info.st_mtime_ns, info.st_size):
                self.pictures.move_to_end(key)
                return hit[0] // 1000, hit[2], hit[3]
            # Capture writers rename atomically. Stat the open file, not the name that may already be newer.
            with path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                body, seq = picture(handle.read(), kind)
            self.pictures[key] = (info.st_mtime_ns, info.st_size, body, seq)
            self.pictures.move_to_end(key)
            while len(self.pictures) > 64:
                self.pictures.popitem(last=False)
            return info.st_mtime_ns // 1000, body, seq

    def row(self, run: Path, record: dict[str, Any]) -> dict[str, Any]:
        """One runs[] row (contract) of a run folder and its record."""
        running = record.get("status") == "running"
        taken = driven(run)
        return {
            "id": run.name,
            "movie": record.get("movie", ""),
            "status": "running" if running else "done",
            "speed": int(record.get("speed") or 0),
            "realtime": realtime(run, record),
            "poll": tas.last_poll(run / tas.HASH) if running else int(record.get("polls_reached") or 0),
            "polls": int(record.get("polls") or 0),
            "outcome": None if running else record.get("outcome"),
            "subject": None if running else record.get("subject") or None,
            "started": record.get("started", 0),
            "ended": None if running else record.get("ended"),
            "label": record.get("label") or None,
            "port": record.get("port") or str(self.cfg.get("port") or ""),
            "input": "human" if taken else "movie",
            "took_at": self.took_at(run, record)
            if taken and (record.get("from") or record.get("input") != "human")
            else None,
            "stopped": bool(record.get("stopped")),
            "from": record.get("from") or None,
            "at": record.get("at"),
            "saved": record.get("saved") or None,
            "pinned": (run / tas.PINNED).exists(),
            "pak": record.get("pak") or "on",
            "cpak": record.get("cpak") or "none",
            "fps_unlock": bool(record.get("fps_unlock")),
            "pads": int(record.get("pads") or 1),
            "shown": record.get("shown") or None,
            "oracle": self.oracle(run, record, running),
        }

    def took_at(self, run: Path, record: dict[str, Any]) -> int | None:
        """The poll live input took a movie run over at (a retry: its `at`), None while the movie still drives it."""
        if record.get("at") is not None:
            return int(record["at"])
        return self._cached(("took", run.name, record.get("status")), lambda: tas.took_over_at(run) or None)

    def oracle(self, run: Path, record: dict[str, Any], running: bool) -> dict[str, Any] | None:
        if not isinstance(record.get("oracle"), dict):
            return None
        if running:
            return tas_oracle.summary(run, record)
        try:
            stamp = (run / tas.RECORD).stat().st_mtime_ns
        except OSError:
            stamp = 0
        hit = self.oracles.get(run.name)
        if not hit or hit[0] != stamp:
            hit = (stamp, tas_oracle.summary(run, record))
            self.oracles[run.name] = hit
        return hit[1]

    def port_format(self) -> int | None:
        """`format` of port.json in the page port's source (tas.serve.port_source); None when absent or unreadable."""
        if not self.port_source:
            return None
        try:
            value = json.loads((self.port_source / "port.json").read_text()).get("format")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def state(self) -> dict[str, Any]:
        """The body of live.json: running and queued runs and the newest finished ones (the contract run row minus
        its port path), the build and the clock."""
        rows = tas.run_rows(self.cfg)
        on_disk = {row["id"] for _, row in rows if "id" in row}
        with self.lock:
            for run_id in [k for k in self.queued if k in on_disk]:
                del self.queued[run_id]
            queued = list(self.queued.values())
        live = [(run, row) for run, row in rows if row.get("status") == "running"]
        done = [(run, row) for run, row in rows if row.get("status") != "running"][-RECENT_SHOWN:]
        runs = [
            {k: v for k, v in r.items() if k != "port"}
            for r in [self.row(run, row) for run, row in live + done] + queued
        ]
        runs.sort(key=lambda r: r["started"])
        port = self.cfg.get("port")
        try:
            built = port.stat().st_mtime
        except (OSError, AttributeError):
            built = 0
        return {
            "now": time.time(),
            "build": {"port": str(port), "pin": self.pin_sha(), "built": built, "format": self.port_format()},
            "runs": runs,
        }

    def running_runs(self) -> list[Path]:
        """The folders of the runs going now (at most a second old: the wall asks 4 times a second)."""
        return self._cached(
            "running", lambda: [run for run, row in tas.run_rows(self.cfg) if row.get("status") == "running"], 1.0
        )

    def history(self, before: float | None, limit: int) -> dict[str, Any]:
        """Finished runs newest first, older than `before` (a `started` time), `limit` at most; `more` when older
        ones remain."""
        rows = [
            (run, row)
            for run, row in reversed(tas.run_rows(self.cfg))
            if row.get("status") != "running" and (before is None or float(row.get("started") or 0) < before)
        ]
        limit = max(1, min(limit, HISTORY_PAGE))
        return {"runs": [self.row(run, row) for run, row in rows[:limit]], "more": len(rows) > limit}

    def movies(
        self, rows: list[tuple[Path, dict[str, Any]]] | None = None, inbox: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Every kept movie's row (what the baked movies.json is made from)."""
        rows = tas.run_rows(self.cfg) if rows is None else rows
        inbox = tas_board.inbox_lines(self.inbox) if inbox is None else inbox
        try:
            standing = json.loads((self.cfg["runs"] / "standing.json").read_text())
        except (OSError, ValueError):
            standing = {}
        latest: dict[str, dict[str, Any]] = {}
        oracle_max: dict[str, float | None] = {}
        for _, row in rows:
            if row.get("status") == "done" and row.get("outcome") and not row.get("stopped") and not row.get("taken"):
                latest[row.get("movie", "")] = row
                if isinstance(row.get("oracle"), dict):
                    oracle_max[row.get("movie", "")] = row["oracle"].get("max_diff")
        folder = self.cfg["movies"]
        out = []
        for path in sorted(folder.glob("*.tas")) if folder.is_dir() else []:
            name = path.stem
            last = latest.get(name)
            known = last or standing.get(name) or {}
            fn = last.get("fn") if last else tas.fn_of(known)
            train = None
            if fn:
                hits = [row for row in inbox if fn in row["line"]]
                train = {"line": hits[-1]["line"], "mark": hits[-1]["mark"]} if hits else None
            meta = self.movie_meta(path)
            out.append(
                {
                    "name": name,
                    "polls": meta["polls"],
                    "root": meta["root"],
                    "from": meta["from"],
                    "at": meta["at"],
                    "outcome": known.get("outcome") or "",
                    "subject": known.get("subject") or None,
                    "reached": int(known.get("polls_reached") or 0),
                    "same_twice": last.get("same_twice") if last else None,
                    "fn": {"name": fn, "state": self.fn_state(fn)} if fn else None,
                    "train": train,
                    "last_run": last.get("ended") if last else None,
                    "label": last.get("label") if last else None,
                    "kind": known.get("kind") or None,
                    "oracle_max": oracle_max.get(name),
                }
            )
        return out

    def _placeholder(
        self,
        run_id: str,
        movie: str,
        speed: int,
        port: Path,
        label: str | None,
        pak: str | None = None,
        cpak: str | None = None,
        fps_unlock: bool = False,
    ) -> None:
        polls = 0
        with contextlib.suppress(OSError):
            polls = self.movie_meta(tas._movie(self.cfg, movie))["polls"]
        if pak is None:  # a replay keeps what its movie ran with
            with contextlib.suppress(DecompError, OSError):
                pak = tas.movie_pak(self.cfg, movie)
        if cpak is None:
            with contextlib.suppress(DecompError, OSError):
                cpak = tas.movie_cpak(self.cfg, movie)
        elif cpak == "off":
            cpak = "none"
        elif cpak == "on":
            cpak = "fresh"
        row = {"id": run_id, "movie": movie, "status": "queued", "speed": speed, "poll": 0, "polls": polls}
        row.update(cpak=cpak or "none", outcome=None, subject=None, started=time.time(), ended=None)
        row.update(
            label=label or None, port=str(port), input="movie", took_at=None, stopped=False, saved=None, pinned=False
        )
        row.update(pak=pak or "on", oracle=None, fps_unlock=fps_unlock, shown=None, **{"from": None, "at": None})
        with self.lock:
            self.queued[run_id] = row
        self.reach.expect(self.cfg["runs"] / "plays" / run_id, lambda: run_id in self.queued)
        self.live.touch()

    def _forget(self, run_id: str) -> None:
        with self.lock:
            self.queued.pop(run_id, None)
        self.live.touch()

    def human_running(self) -> str | None:
        """The run live input drives now, if any (one at a time)."""
        for run, row in tas.run_rows(self.cfg):
            if row.get("status") == "running" and driven(run):
                return run.name
        with self.lock:
            return next((k for k, r in self.queued.items() if r.get("input") == "human"), None)

    def new_human(
        self,
        port: str | None,
        label: str | None,
        pak: str | None = None,
        cpak: str | None = None,
        fps_unlock: bool = False,
    ) -> dict[str, Any]:
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        tas.check_pak(pak)
        tas.check_cpak(cpak)
        exe = tas.with_port(self.cfg, port)["port"]
        movie = tas.human_movie_name()
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
        self._placeholder(run_id, movie, 1, exe, label or tas.HUMAN_LABEL, pak or "on", cpak or "on", fps_unlock)
        with self.lock:
            self.queued[run_id]["input"] = "human"
        self.live.touch()

        def go() -> None:
            try:
                tas.play(
                    self.profile,
                    None,
                    speed=1,
                    run_id=run_id,
                    port=port or None,
                    label=label or None,
                    pak=pak,
                    cpak=cpak,
                    fps_unlock=fps_unlock,
                )
            except Exception:
                pass
            finally:
                self._forget(run_id)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": movie}

    def take(self, run_id: str) -> dict[str, Any]:
        run = self.run_dir(run_id)
        if not run or not tas.running(run):
            raise DecompError("tas.take", f"no running run {run_id}")
        busy = self.human_running()
        if busy and busy != run_id:
            raise DecompError("tas.take", f"live input already drives {busy}; stop it first")
        (run / tas.TAKEN).write_text("page\n")
        (run / RELEASED).unlink(missing_ok=True)
        self.send(run, tas.TAKE_DATAGRAM)
        self.live.touch()
        return {"id": run_id, "taken": True}

    def release(self, run_id: str) -> dict[str, Any]:
        """Give a taken run back: it returns to turbo with neutral input, and another run may be taken. `taken` stays
        (the run is still saved as a take); `released` says live input no longer drives it."""
        run = self.run_dir(run_id)
        if not run or not driven(run):
            raise DecompError("tas.release", f"run {run_id} is not driven by live input")
        self.send(run, tas.RELEASE_DATAGRAM)
        (run / RELEASED).write_text("page\n")
        self.live.touch()
        return {"id": run_id, "released": True}

    def input(self, run_id: str, seq: int, buttons: int, x: int, y: int, pad: int = 0) -> dict[str, Any]:
        run = self.run_dir(run_id)
        if not run or not driven(run):
            raise DecompError("tas.input", f"run {run_id} is not driven by live input")
        if not (0 <= buttons <= 0xFFFF and -128 <= x <= 127 and -128 <= y <= 127 and seq >= 0):
            raise DecompError("tas.input", "buttons 0..0xffff, x and y -128..127, seq >= 0")
        pads = int((tas.read_record(run) or {}).get("pads") or 1)
        if not 0 <= pad < pads:
            raise DecompError("tas.input", f"pad {pad} of {pads}: this run has {pads} controller(s), numbered from 0")
        self.send(run, f"{seq} {buttons:x} {x} {y}{f' {pad}' if pad else ''}".encode())
        return {"ok": True}

    def send(self, run: Path, data: bytes) -> None:
        try:
            tas.send_input(run, data)
        except OSError as error:
            raise DecompError("tas.input", f"the run's input socket is not there yet: {error}") from error

    def play(
        self,
        movie: str,
        speed: int,
        port: str | None = None,
        label: str | None = None,
        pak: str | None = None,
        oracle: bool = False,
        cpak: str | None = None,
        source: str | None = None,
        take_at: int | None = None,
        fps_unlock: bool = False,
        start: str | None = None,
        take: bool = False,
    ) -> dict[str, Any]:
        if source:
            return self.retry(source, take_at, port, label, fps_unlock)
        if start and take and not movie:
            return self.from_point(start, port, label, fps_unlock)
        if start and not movie:
            movie, start = start, None  # the point alone, replayed like a movie
        if not movie:
            return self.new_human(port, label, pak, cpak, fps_unlock)
        tas.movie_polls(self.cfg, movie, start)  # refuse a missing movie or another ROM here, not in the thread
        if oracle:
            tas_oracle.required(self.profile)
        if not 0 <= speed <= 1000:
            raise DecompError("tas.play", f"speed is 0 (turbo) .. 1000, not {speed}")
        tas.check_label(label)
        tas.check_pak(pak)
        tas.check_cpak(cpak)
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
            while run_id in self.queued:
                run_id += "-q"
        self._placeholder(run_id, movie, speed, exe, label, pak, cpak, fps_unlock)

        def go() -> None:
            try:
                options = {
                    "port": port or None,
                    "label": label or None,
                    "pak": pak,
                    "cpak": cpak,
                    "oracle": oracle,
                    "oracle_pictures": oracle,  # a person asked from the page: the picture comparison is the point
                    "fps_unlock": fps_unlock,
                }
                tas.play(self.profile, movie, speed=speed, run_id=run_id, start=start, **options)
            except Exception:  # the record says done; the page shows it
                pass
            finally:
                self._forget(run_id)

        self.pool.submit(go)
        return {"id": run_id}

    def from_point(self, start: str, port: str | None, label: str | None, fps_unlock: bool = False) -> dict[str, Any]:
        """A new live run at start point `start`: the point's polls at turbo, then live input (tas.play take)."""
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        point = tas_points.load(self.cfg, start)  # refuse a missing point here, not in the thread
        exe = tas.with_port(self.cfg, port)["port"]
        movie = tas.human_movie_name()
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
        cpak, pak = point["header"].get("cpak", "none"), point["header"].get("pak", "on")
        self._placeholder(run_id, movie, 0, exe, label or tas.HUMAN_LABEL, pak, cpak, fps_unlock)
        with self.lock:
            self.queued[run_id].update(
                input="human",
                took_at=len(point["polls"]),
                polls=len(point["polls"]),
                pads=tas.pad_count(point["polls"]),
            )
        self.live.touch()

        def go() -> None:
            try:
                tas.play(
                    self.profile,
                    None,
                    run_id=run_id,
                    port=port or None,
                    label=label or None,
                    start=start,
                    take=True,
                    fps_unlock=fps_unlock,
                )
            except Exception:
                pass
            finally:
                self._forget(run_id)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": movie, "start": start}

    def retry(
        self, source: str, take_at: int | None, port: str | None, label: str | None, fps_unlock: bool = False
    ) -> dict[str, Any]:
        """Retry run `source` from poll `take_at`: its recording to that poll at turbo, then live input (tas.play)."""
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        found = tas.retry_source(self.cfg, source, take_at)  # refuse a bad run or poll here, not in the thread
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, found["movie"])
        self._placeholder(
            run_id, found["movie"], 0, exe, label or tas.HUMAN_LABEL, found["pak"], found["cpak"], fps_unlock
        )
        with self.lock:
            self.queued[run_id].update(input="human", took_at=take_at, polls=len(found["polls"]), pads=found["pads"])
            self.queued[run_id].update({"from": source, "at": take_at})
        self.live.touch()

        def go() -> None:
            try:
                tas.play(
                    self.profile, None, run_id=run_id, port=port or None, label=label or None, source=source,
                    take_at=take_at, fps_unlock=fps_unlock,
                )  # fmt: skip
            except Exception:
                pass
            finally:
                self._forget(run_id)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": found["movie"]}

    def keep(self, run_id: str, keep: bool) -> dict[str, Any]:
        out = tas.pin(self.cfg, run_id, keep)
        self.live.touch()
        return out

    def archive(self) -> None:
        """Every ARCHIVE_SECONDS: delete runs past keep_days (tas.archive); a failure waits for the next round."""
        while True:
            with contextlib.suppress(OSError, DecompError):
                gone = tas.archive(self.cfg, self.keep_days)
                if gone:
                    print(f"tas page: archived {len(gone)} runs older than {self.keep_days:g} days", flush=True)
            time.sleep(ARCHIVE_SECONDS)

    def run_dir(self, run_id: str) -> Path | None:
        return tas.run_folder(self.cfg, run_id)

    def stop(self, run_id: str) -> dict[str, Any]:
        run = self.run_dir(run_id)
        row = tas.read_record(run) if run else None
        if not run or not row:
            raise DecompError("tas.stop", f"no run {run_id}")
        if row.get("status") != "running" or not row.get("pgid"):
            return {"id": run_id, "stopped": False}
        (run / "stopped").write_text("page\n")
        group = int(row["pgid"])
        try:
            os.killpg(group, signal.SIGTERM)
        except (OSError, ValueError):
            return {"id": run_id, "stopped": False}
        # The gdb bridge can outlive a TERM while it writes stacks; the run's whole group goes after STOP_GRACE.
        threading.Timer(STOP_GRACE, self._kill, (run, group)).start()
        return {"id": run_id, "stopped": True}

    @staticmethod
    def _kill(run: Path, group: int) -> None:
        if tas.running(run):
            with contextlib.suppress(OSError):
                os.killpg(group, signal.SIGKILL)


class Live:
    """live.json (`<site>/live.json`): the body of `Server.state()` written whenever the runs change, never per request.
    An inotify watch on `runs/plays` and on every folder of a run that is not done reports a new run, a rewritten
    `run.json`, and the `taken` / `stopped` / `pinned` marks; while anything runs the file is also refreshed every
    LIVE_SECONDS for the poll counter (the port appends to `hash.txt` without closing it, so that file raises no event).
    Writes are at most one per LIVE_GAP_SECONDS and carry no change of their own when only the clock moved."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.path = server.site / "live.json"
        self.wake_r, self.wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.last = b""
        self.running: list[Path] = []
        self.watched: set[Path] = set()
        self.watcher: inotify.Watcher | None = None

    def touch(self) -> None:
        """Something the watch cannot see changed (a queued run, a pin): rewrite soon."""
        with contextlib.suppress(OSError):
            os.write(self.wake_w, b"x")

    def write(self) -> bool:
        """Write live.json if its content changed (not just `now`); True when it did. Atomic: temp file + rename."""
        doc = self.server.state()
        self.running = [self.server.cfg["runs"] / "plays" / r["id"] for r in doc["runs"] if r["status"] == "running"]
        body = json.dumps({**doc, "now": 0}, separators=(",", ":")).encode()
        if body == self.last and self.path.is_file():
            return False
        self.last = body
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(json.dumps(doc, separators=(",", ":")).encode())
        tmp.replace(self.path)
        return True

    def sync(self) -> None:
        """Watch the folders of runs that are not done, drop the others, and give the reach relay its running set."""
        watcher, plays = self.watcher, self.server.cfg["runs"] / "plays"
        if watcher is None:
            return
        for run in sorted(plays.iterdir()) if plays.is_dir() else []:
            if run in self.watched or not run.is_dir():
                continue
            record = tas.read_record(run)
            if record is None or record.get("status") == "running":
                with contextlib.suppress(OSError):
                    watcher.add(run, inotify.CREATE | inotify.MOVED_TO | inotify.CLOSE_WRITE | inotify.DELETE)
                    self.watched.add(run)
        for run in list(self.watched):
            record = tas.read_record(run) if run.is_dir() else None
            if record is not None and record.get("status") != "running":
                watcher.remove(run)
                self.watched.discard(run)
        self.server.reach.sync([run for run in self.watched if tas.running(run)])

    def relevant(self, events: list[tuple[Path, int]]) -> bool:
        plays = self.server.cfg["runs"] / "plays"
        for path, _mask in events:
            if path.parent == plays or path.name in (tas.RECORD, tas.TAKEN, RELEASED, "stopped", tas.PINNED):
                return True
        return False

    def loop(self) -> None:
        plays = self.server.cfg["runs"] / "plays"
        plays.mkdir(parents=True, exist_ok=True)
        self.watcher = inotify.Watcher()
        self.watcher.add(plays, inotify.CREATE | inotify.MOVED_TO | inotify.DELETE)
        self.sync()
        dirty, written = True, -LIVE_IDLE_SECONDS
        while True:
            now = time.monotonic()
            beat = LIVE_SECONDS if self.running else LIVE_IDLE_SECONDS
            if (dirty and now - written >= LIVE_GAP_SECONDS) or now - written >= beat:
                self.sync()  # also finds a run that died without a record, and drops watches of finished ones
                self.write()
                dirty, written = False, now
            wait = max(0.0, written + (LIVE_GAP_SECONDS if dirty else beat) - time.monotonic())
            ready = select.select([self.watcher.fd, self.wake_r], [], [], wait)[0]
            if self.wake_r in ready:
                inotify.drain(self.wake_r)
                dirty = True
            if self.watcher.fd in ready and self.relevant(self.watcher.read()):
                self.sync()  # a new run's reader must listen before its port sends its first record
                dirty = True

    def run(self) -> None:
        while True:
            try:
                self.loop()
            except Exception as error:  # a bad record or a full disk must not end the writer: start the watch again
                print(f"tas page: live.json writer: {error}", flush=True)
                time.sleep(1)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="live").start()


def varint(data: bytes, pos: int) -> tuple[int, int]:
    """One LEB128 integer of a reach record at `pos`: (value, next position); ValueError when it is cut off."""
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 28:
            raise ValueError("bad varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7


def reach_rows(data: bytes, pos: int) -> tuple[list[int], int]:
    count, pos = varint(data, pos)
    rows, row = [], 0
    for _ in range(count):
        step, pos = varint(data, pos)
        row += step
        if row >= REACH_MAX_ROW:
            raise ValueError("row out of range")
        rows.append(row)
    return rows, pos


def reach_full(poll: int, bits: bytes) -> bytes:
    """A full reach record (type 1): the poll and the bitset over fns.json rows."""
    return struct.pack("<BI", 1, poll) + bytes(bits)


class RunReach:
    """One run's reach stream: the cumulative bitset, the poll of the last record and the records not yet forgotten."""

    def __init__(self, run: Path, pending: Callable[[], bool] = lambda: False) -> None:
        self.run = run
        self.pending = pending  # true while the run is queued: its folder and port are not there yet, so keep listening
        self.lock = threading.Lock()
        self.bits = bytearray()
        self.poll = 0
        self.count = 0  # records accepted
        self.rejected = 0
        self.seq = 0  # sequence number of the newest kept record
        self.records: deque[tuple[int, bytes]] = deque()
        self.size = 0
        self.done = False
        self.idle = 0  # reads that timed out with nothing to receive
        self.listening = threading.Event()  # set once the socket is bound: the port's datagrams are no longer dropped

    def feed(self, data: bytes) -> bool:
        """Apply one datagram. A malformed one is dropped (False), never half applied."""
        try:
            if len(data) < 5 or data[0] not in (1, 2):
                raise ValueError("not a reach record")
            poll = struct.unpack_from("<I", data, 1)[0]
            if data[0] == 2:
                fresh, pos = reach_rows(data, 5)
                hot, pos = reach_rows(data, pos)
                if pos != len(data):
                    raise ValueError("trailing bytes")
        except ValueError:
            with self.lock:
                self.rejected += 1
            return False
        with self.lock:
            if data[0] == 1:
                self.bits = bytearray(data[5:])
            else:
                for row in fresh + hot:
                    if row >> 3 >= len(self.bits):
                        self.bits.extend(bytes((row >> 3) + 1 - len(self.bits)))
                    self.bits[row >> 3] |= 1 << (row & 7)
            self.poll, self.count, self.seq = poll, self.count + 1, self.seq + 1
            self.records.append((self.seq, bytes(data)))
            self.size += len(data)
            while len(self.records) > REACH_KEPT_RECORDS or self.size > REACH_KEPT_BYTES:
                self.size -= len(self.records.popleft()[1])
        return True

    def snapshot(self) -> tuple[bytes, int]:
        """(a full record of everything so far, the sequence number it is current to), taken together."""
        with self.lock:
            return reach_full(self.poll, self.bits), self.seq

    def since(self, seq: int) -> tuple[list[bytes], int]:
        """(records after `seq` in order, the newest sequence number). A reader that fell behind what is kept gets
        one fresh full record instead of the gap."""
        with self.lock:
            if self.seq == seq:
                return [], seq
            if not self.records or self.records[0][0] > seq + 1:
                return [reach_full(self.poll, self.bits)], self.seq
            return [r for n, r in self.records if n > seq], self.seq

    def finish(self) -> None:
        """Write RUN/reach.bin: the final bitset, if the run sent any record. Atomic, like run.json."""
        with self.lock:
            bits, got = bytes(self.bits), self.count
        if got:
            with contextlib.suppress(OSError):  # the run folder was archived under the reader
                tmp = self.run / (REACH_FILE + ".tmp")
                tmp.write_bytes(bits)
                tmp.replace(self.run / REACH_FILE)
        self.done = True

    def serve(self) -> None:
        """The reader: datagrams on the run's reach socket (the input socket's name + `.reach`, abstract namespace;
        the port sends, non-blocking, and drops when nobody listens) until the run ends, then reach.bin."""
        address = "\0" + tas.input_address(self.run)[1:] + ".reach"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.bind(address)
            except OSError as error:  # another serve already listens for this run
                print(f"tas page: no reach stream for {self.run.name}: {error}", flush=True)
                self.done = True
                return
            sock.settimeout(REACH_READ_TIMEOUT)
            self.listening.set()
            while True:
                try:
                    self.feed(sock.recv(REACH_MAX_RECORD + 1))
                    continue
                except TimeoutError:
                    self.idle += 1
                    if tas.running(self.run) or self.pending():
                        continue
                except OSError:
                    pass
                break
            sock.settimeout(0)
            while True:  # whatever the port sent before it exited
                try:
                    self.feed(sock.recv(REACH_MAX_RECORD + 1))
                except OSError:
                    break
        self.finish()


class ReachFeed:
    """One /video viewer's cursor on a run's reach records: a full record first, then each later one once, in order."""

    def __init__(self, relay: RunReach | None, final: bytes | None = None) -> None:
        self.relay, self.final = relay, final
        self.seq = 0

    def first(self) -> bytes | None:
        if self.relay is None:
            return self.final
        record, self.seq = self.relay.snapshot()
        return record

    def pending(self) -> list[bytes]:
        if self.relay is None:
            return []
        records, self.seq = self.relay.since(self.seq)
        return records


class Reach:
    """The reach relay of all runs (GAME-RUNNER 10a): a RunReach per running run, kept a while after the run ends."""

    def __init__(self) -> None:
        self.runs: OrderedDict[str, RunReach] = OrderedDict()
        self.lock = threading.Lock()

    def sync(self, running: list[Path]) -> None:
        """Listen for every running run that has no reader yet."""
        with self.lock:
            for run in running:
                if run.name not in self.runs:
                    relay = self.runs[run.name] = RunReach(run)
                    threading.Thread(target=relay.serve, daemon=True, name=f"reach-{run.name}").start()
            while len(self.runs) > REACH_RUNS_KEPT:
                oldest = next(iter(self.runs))
                if not self.runs[oldest].done:
                    break
                del self.runs[oldest]

    def expect(self, run: Path, pending: Callable[[], bool]) -> None:
        """Listen for a run that has not started: the port's first full record is dropped when nobody listens yet, so
        the socket is bound when the run is queued, and stays bound while `pending` says the run is still queued."""
        with self.lock:
            if run.name not in self.runs:
                relay = self.runs[run.name] = RunReach(run, pending)
                threading.Thread(target=relay.serve, daemon=True, name=f"reach-{run.name}").start()

    def feed_for(self, run: Path) -> ReachFeed | None:
        """The records of `run` for one viewer: its live relay, else the final set a finished run left in reach.bin
        (sent once as a full record), else None."""
        with self.lock:
            relay = self.runs.get(run.name)
        if relay is not None:
            return ReachFeed(relay)
        try:
            bits = (run / REACH_FILE).read_bytes()
        except OSError:
            return None
        record = tas.read_record(run) or {}
        return ReachFeed(None, reach_full(int(record.get("polls_reached") or 0), bits))


def body_tag(body: bytes) -> str:
    return '"' + hashlib.blake2b(body, digest_size=10).hexdigest() + '"'


def tag_matches(header: str | None, tag: str) -> bool:
    """An If-None-Match header (a list, weak tags allowed) names `tag`."""
    return bool(header) and any(part.strip().removeprefix("W/") in (tag, "*") for part in str(header).split(","))


def handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            # A frame/PCM fragment must not wait for a delayed ACK on HTTP/1.1 phones.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *args: Any) -> None:
            pass

        def reply(
            self,
            body: bytes,
            kind: str,
            status: int = HTTPStatus.OK,
            tag: str | None = None,
            extra: dict[str, str] | None = None,
        ) -> None:
            """Send a body. Text kinds carry an ETag (`tag`, else a hash of the body): a matching If-None-Match gets
            a 304 with no body, and a client that accepts gzip gets the compressed body. Others are sent as they are."""
            headers = dict(extra or {})
            text = kind.split(";")[0] in TEXT_KINDS and status == HTTPStatus.OK
            if text:
                tag = tag or body_tag(body)
                headers["ETag"] = tag
                headers["Vary"] = "Accept-Encoding"
                if tag_matches(self.headers.get("If-None-Match"), tag):
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                if len(body) >= GZIP_MIN and "gzip" in self.headers.get("Accept-Encoding", ""):
                    body = server.gzipped(tag, body)
                    headers["Content-Encoding"] = "gzip"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache" if text else "no-store")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            self.reply(json.dumps(value).encode(), "application/json", status)

        def missing(self) -> None:
            self.json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            head, _, run_id = path[1:].partition("/")
            try:
                if path == "/":
                    self.reply(PAGE().encode(), "text/html; charset=utf-8")
                elif path == "/api/coverage":
                    self.json(server.coverage())
                elif path == "/api/pool":
                    self.json(server.slot_pool())
                elif path == "/api/runs":
                    args = dict(part.partition("=")[::2] for part in query.split("&") if part)
                    before = float(args["before"]) if args.get("before") else None
                    self.json(server.history(before, int(args.get("limit") or HISTORY_PAGE)))
                elif path == "/api/phase":
                    self.json(*server.phase.phase())
                elif path == "/phase/shot":
                    self.phase_shot(query)
                elif path == "/phase/frame.png":
                    self.phase_frame(query)
                elif head == "data" and run_id:
                    self.data(run_id)
                elif path == "/wall":
                    self.wall()
                elif head == "static" and run_id:
                    self.static(STATIC, run_id)
                elif head == "coverage":
                    self.static(server.cfg["runs"] / tas_coverage.HTML, run_id or "index.html")
                elif head == "video" and run_id:
                    self.video(server.run_dir(run_id))
                elif head == "frame" and run_id:
                    self.frame(server.run_dir(run_id))
                elif head == "oracle" and run_id:
                    self.frame(server.run_dir(run_id), tas_oracle.VIEW)
                elif head == "shot" and run_id:
                    self.shot(server.run_dir(run_id), query)
                elif head == "diff" and run_id:
                    self.diff(server.run_dir(run_id))
                elif head == "log" and run_id:
                    self.log(server.run_dir(run_id), query)
                elif head == "audio" and run_id:
                    self.audio(server.run_dir(run_id), query)
                else:
                    self.missing()
            except (BrokenPipeError, ConnectionResetError):
                return

        def phase_shot(self, query: str) -> None:
            platform = dict(p.partition("=")[::2] for p in query.split("&") if p).get("platform") or ""
            found = server.phase.shot(platform)
            if found is None:
                self.json({"error": "no screenshot for that platform"}, HTTPStatus.NOT_FOUND)
            else:
                self.reply(found[0], found[1])

        def phase_frame(self, query: str) -> None:
            version = dict(p.partition("=")[::2] for p in query.split("&") if p).get("version") or server.phase.focus()
            image, why = server.phase.frame(version) if version else (None, "no version")
            if image is None:
                self.json({"error": why}, HTTPStatus.NOT_FOUND)
            else:
                self.reply(image, "image/png")

        def static(self, root: Path, name: str) -> None:
            """A file below `root` (the gcovr HTML report, the page's static files); a name that leaves it is a 404."""
            path = (root / name).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                return self.missing()
            kind = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css",
                ".js": "text/javascript",
                ".svg": "image/svg+xml",
            }
            self.reply(path.read_bytes(), kind.get(path.suffix, "application/octet-stream"))

        def frame(self, run: Path | None, name: str = tas.VIEW) -> None:
            path = run / name if run else None
            try:
                stamp, body, seq = server.capture(path)  # type: ignore[arg-type]
            except (OSError, AttributeError, ValueError):
                return self.missing()
            return self.reply(body, "image/png", extra={"X-Frame": str(stamp), "X-Input-Seq": str(seq)})

        def video(self, run: Path | None) -> None:
            """One bounded latest-frame stream: 30 samples/s, independent of RTT and turbo's game clock."""
            if not run:
                return self.missing()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-tas-frames")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Video-Fps", str(VIDEO_FPS))
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            # A stalled phone cannot accumulate seconds of pictures in the server's socket buffer.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            self.connection.settimeout(5)
            stamp, deadline, sent_seq = 0, time.monotonic(), 0
            feed = server.reach.feed_for(run)  # the run's reach records (section 10a), None when it has none
            first = feed.first() if feed else None
            while True:
                try:
                    current, body, seq = server.capture(run / tas.VIEW, "JPEG")
                except (OSError, ValueError):
                    current, body, seq = stamp, b"", 0
                changed = current != stamp
                # Reach records go out ahead of the picture they belong to; a new viewer's first bytes are a full one.
                records = ([first] if first else []) + (feed.pending() if feed else [])
                first = None
                out = b"".join(VIDEO_HEADER.pack(len(r) | REACH_FLAG, current, sent_seq) + r for r in records)
                if changed:
                    out += VIDEO_HEADER.pack(len(body), current, seq) + body
                if out:
                    try:
                        self.wfile.write(out)
                        self.wfile.flush()
                    except OSError:
                        return  # disconnect/backpressure timeout: release this viewer immediately
                if changed:
                    stamp, sent_seq = current, seq
                if not tas.running(run):
                    return
                now = time.monotonic()
                if changed:
                    deadline = max(deadline + 1 / VIDEO_FPS, now)
                # When the producer is late, wait for its next capture without losing another whole period.
                time.sleep(max(0.002, deadline - now))

        def wall(self) -> None:
            """One multiplexed stream of WALL_WIDTH px JPEG thumbs of every running run, WALL_FPS a second. A frame is
            VIDEO_HEADER (length, stamp, run index in the place of the input sequence) + JPEG. The first time a run
            shows, a record with WALL_TABLE set in its length (body: JSON {"id": run id}) names its index; indices are
            per connection and never reused. Each run's reach records (section 10a) ride flagged under its index too, a
            full one first: the body of a flagged record is the table when it starts with "{", else a reach record.
            Browsers allow six connections per host, so twelve per-tile streams would stall."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-tas-wall")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Wall-Fps", str(WALL_FPS))
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            self.connection.settimeout(5)
            index: dict[str, int] = {}
            stamps: dict[str, int] = {}
            feeds: dict[str, ReachFeed | None] = {}
            quiet = due = time.monotonic()
            while True:
                out = b""
                for run in server.running_runs():
                    try:
                        stamp, body, _ = server.capture(run / tas.VIEW, "THUMB")
                    except (OSError, ValueError):
                        continue
                    if run.name not in index and len(index) < WALL_KEEPALIVE_INDEX:
                        index[run.name] = len(index)
                        name = json.dumps({"id": run.name}).encode()
                        out += VIDEO_HEADER.pack(len(name) | WALL_TABLE, 0, index[run.name]) + name
                        feeds[run.name] = feed = server.reach.feed_for(run)
                        first = feed.first() if feed else None
                        if first and any(first[5:]):  # a run that lit nothing yet costs the wall no record
                            out += VIDEO_HEADER.pack(len(first) | REACH_FLAG, stamp, index[run.name]) + first
                    feed = feeds.get(run.name)
                    for record in feed.pending() if feed else []:
                        out += VIDEO_HEADER.pack(len(record) | REACH_FLAG, stamp, index[run.name]) + record
                    if run.name in index and stamps.get(run.name) != stamp:
                        stamps[run.name] = stamp
                        out += VIDEO_HEADER.pack(len(body), stamp, index[run.name]) + body
                now = time.monotonic()
                if not out and now - quiet >= WALL_KEEPALIVE_SECONDS:
                    out = VIDEO_HEADER.pack(WALL_TABLE, 0, WALL_KEEPALIVE_INDEX)
                if out:
                    quiet = now
                    try:
                        self.wfile.write(out)
                        self.wfile.flush()
                    except OSError:
                        return
                due = max(due + 1 / WALL_FPS, time.monotonic())  # a round's own work does not slow the pace
                time.sleep(max(0.0, due - time.monotonic()))

        def data(self, name: str) -> None:
            """A file of the baked site (`<tas.runs>/site/`). A client that accepts gzip gets the `.gz` sibling the bake
            wrote (else a body over GZIP_MIN is compressed once per tag); hashed names are immutable, live.json and
            manifest.json are never stored, the rest revalidate by ETag."""
            rel = urllib.parse.unquote(name)
            root = server.site.resolve()
            target = (root / rel).resolve()
            if root not in target.parents or target.suffix == ".gz" or not target.is_file():
                return self.missing()
            kind = DATA_KINDS.get(target.suffix, "application/octet-stream")
            zipped = target.with_name(target.name + ".gz")
            packed = "gzip" in self.headers.get("Accept-Encoding", "") and zipped.is_file()
            try:
                info = (zipped if packed else target).stat()
                body = (zipped if packed else target).read_bytes()
            except OSError:
                return self.missing()
            tag = f'"{info.st_mtime_ns:x}-{info.st_size:x}{"z" if packed else ""}"'
            headers = {"ETag": tag, "Vary": "Accept-Encoding"}
            if target.name in NO_STORE_DATA:
                headers["Cache-Control"] = "no-store"
            elif HASHED_NAME.search(target.name):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                headers["Cache-Control"] = "no-cache"
            if tag_matches(self.headers.get("If-None-Match"), tag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                return
            if packed:
                headers["Content-Encoding"] = "gzip"
            elif (
                kind.split(";")[0] in TEXT_KINDS
                and len(body) >= GZIP_MIN
                and "gzip" in self.headers.get("Accept-Encoding", "")
            ):
                body = server.gzipped(f"data{tag}{target.name}", body)
                headers["Content-Encoding"] = "gzip"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def shot(self, run: Path | None, query: str) -> None:
            """One side's picture at one compared poll: ?side=port|oracle&poll=N."""
            args = dict(part.partition("=")[::2] for part in query.split("&"))
            side, poll = args.get("side", ""), args.get("poll", "")
            if not run or side not in tas_oracle.SIDES or not poll.isdigit():
                return self.missing()
            meta = (tas.read_record(run) or {}).get("oracle")
            if side == "oracle" and (cause := tas_oracle.gate_cause(meta)):
                return self.reply(cause.encode(), "text/plain; charset=utf-8", HTTPStatus.CONFLICT)
            try:
                _, body, _ = server.capture(run / tas_oracle.SHOTS / side / f"poll-{int(poll):06d}.ppm")
            except (OSError, ValueError):
                return self.missing()
            return self.reply(body, "image/png")

        def diff(self, run: Path | None) -> None:
            """Every compared poll's score: {"every", "rows": [{"poll", "diff", "drift", "vi_drift"}]}."""
            row = tas.read_record(run) if run else None
            meta = row.get("oracle") if row else None
            if not run or not isinstance(meta, dict):
                return self.missing()
            summary = tas_oracle.summary(run, row) or {}
            every = int(meta.get("every") or 1)
            try:
                rows = tas_oracle.compare(run, every, summary.get("status") == "done", meta)
            except DecompError as error:
                return self.json({"every": every, "rows": [], "refused": error.detail})
            return self.json({"every": every, "rows": rows})

        def log(self, run: Path | None, query: str) -> None:
            if not run:
                return self.missing()
            start = 0
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "from" and value.isdigit():
                    start = int(value)
            try:
                with (run / "output.txt").open("rb") as handle:
                    handle.seek(start)
                    body = handle.read(1 << 20)
            except OSError:
                body = b""
            self.reply(body, "text/plain; charset=utf-8")

        def audio(self, run: Path | None, query: str = "") -> None:
            row = tas.read_record(run) if run else None
            if not run or not row:
                return self.missing()
            if not realtime(run, row):
                return self.reply(b"", "audio/wav", HTTPStatus.NO_CONTENT, extra={"X-Audio-Muted": "not-realtime"})
            name = tas_oracle.AUDIO if "side=oracle" in query.split("&") else tas.AUDIO
            pcm, rate_file = run / name, run / (name + ".rate")
            deadline = time.monotonic() + 10
            while not rate_file.is_file() or not pcm.is_file():
                if not tas.running(run) or time.monotonic() > deadline:
                    return self.missing()
                time.sleep(0.05)
            rate = int(rate_file.read_text().strip() or 32000)
            if not 8000 <= rate <= 192000:
                return self.missing()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(5)
            self.wfile.write(wav_header(rate))
            oracle = name == tas_oracle.AUDIO

            def due() -> int:
                return int(max(0.0, time.time() - float(row.get("started") or 0)) * rate) * 4

            end = min(due(), pcm.stat().st_size) if oracle else pcm.stat().st_size
            offset = max(0, end - int(rate * AUDIO_LEAD_SECONDS) * 4) // 4 * 4
            # Even a producer that writes in bursts gets one steady wall-paced stream. Initial lead covers jitter.
            began, sent = time.monotonic(), 0
            with pcm.open("rb") as handle:
                while True:
                    allowance = int((time.monotonic() - began + AUDIO_LEAD_SECONDS) * rate) * 4 - sent
                    count = min(max(0, allowance), int(rate * 0.05) * 4)
                    if oracle:
                        count = min(count, max(0, due() - offset))
                    handle.seek(offset)
                    chunk = handle.read(count)
                    chunk = chunk[: len(chunk) // 4 * 4]
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        offset += len(chunk)
                        sent += len(chunk)
                    elif not tas.running(run) and (offset >= pcm.stat().st_size or (oracle and offset >= due())):
                        return
                    time.sleep(0.02)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/play":
                    self.json(
                        server.play(
                            str(body.get("movie") or ""),
                            int(body.get("speed", 0)),
                            str(body.get("port") or "") or None,
                            str(body.get("label") or "") or None,
                            str(body.get("pak") or "") or None,
                            bool(body.get("oracle", False)),
                            str(body.get("cpak") or "") or None,
                            str(body.get("from") or "") or None,
                            int(body["at"]) if body.get("at") is not None else None,
                            bool(body.get("fps_unlock", False)),
                            str(body.get("start") or "") or None,
                            bool(body.get("take", False)),
                        )
                    )
                elif self.path == "/api/stop":
                    self.json(server.stop(str(body.get("id", ""))))
                elif self.path == "/api/pin":
                    self.json(server.keep(str(body.get("id", "")), bool(body.get("pinned", True))))
                elif self.path == "/api/take":
                    self.json(server.take(str(body.get("id", ""))))
                elif self.path == "/api/input" and body.get("release"):
                    self.json(server.release(str(body.get("id", ""))))
                elif self.path == "/api/input":
                    self.json(
                        server.input(
                            str(body.get("id", "")),
                            int(body.get("seq", 0)),
                            int(body.get("buttons", 0)),
                            int(body.get("x", 0)),
                            int(body.get("y", 0)),
                            int(body.get("pad", 0)),
                        )
                    )
                else:
                    self.missing()
            except (DecompError, ValueError, TypeError) as error:
                self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve(profile: Profile) -> dict[str, Any]:
    server = Server(profile)
    host, _, port = server.listen.rpartition(":")
    http = ThreadingHTTPServer((host, int(port)), handler(server))
    http.daemon_threads = True
    server.live.start()
    threading.Thread(target=server.archive, daemon=True).start()
    threading.Thread(target=server.gatekeeper, daemon=True).start()
    threading.Thread(target=server.phase.run_frames, daemon=True).start()
    print(f"tas page: http://{server.listen}/", flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return {"listen": server.listen}
