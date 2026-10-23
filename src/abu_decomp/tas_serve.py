"""`decomp tas serve`: the TAS page and the API behind it (orch plans/TAS-PAGE-CONTRACT.md).

Section `tas.serve`: {"listen": "HOST:PORT", "jobs": N, "inbox": path, "states": path, "pin": path, "lanes": path,
"keep_days": days}. Every run (started here, by `tas play` in a shell, or by `play --all`) is a folder under
`runs/plays` whose `run.json` the port's run writes (module tas); the page sees all of them: the port's own latest
picture (`view.bmp`), its log and its sound (`audio.pcm`, served as a growing WAV). `/api/state` carries what is live
(running and queued runs, the newest finished ones, movies, lanes, the train inbox); `/api/runs` pages the whole run
history; `/api/board` the slow side (module tas_board: decomp progress, landed commits, the ranked work list).
Any running run can be taken over by live input (`/api/take`, `/api/input`: datagrams to the run's input socket,
tas.input_address), `/api/play` without a movie starts a human run, and `/api/play` with `from` + `at` retries a run
from a poll (tas.play `source`/`take_at`); one run is driven by live input at a time. `/api/pin` keeps a run; every
hour runs older than `keep_days` (default 3) that are neither pinned nor their movie's newest are deleted
(tas.archive). `inbox` is the integration train's inbox, `states` the decomp progress states, `pin` the git folder of
the decomp source the port was built from, `lanes` the folder of lane dirs mirrored from the orchestrator. `/api/corpus`
is the regression corpus and its last gate (module tas_corpus); serve gates the corpus itself whenever the port binary
is a build it has not been gated on and nothing runs. Function
state (exact / fuzzy / none) is read from the decomp source, as the progress states define it: a NON_MATCHING body is
fuzzy, INCLUDE_ASM alone is none. A run played with the oracle (module tas_oracle) also shows the original game: its
latest picture (/oracle/<id>), both sides' pictures at each compared poll (/shot/<id>) and the scores (/diff/<id>).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from abu_decomp import (
    DecompError,
    evidence,
    tas,
    tas_board,
    tas_buckets,
    tas_corpus,
    tas_coverage,
    tas_findings,
    tas_health,
    tas_oracle,
    tas_points,
    tas_triage,
)
from abu_decomp.config import Profile

_SERVE_KEYS = frozenset({"listen", "jobs", "inbox", "states", "pin", "lanes", "keep_days"})
RECENT_SHOWN = 12  # newest finished runs in /api/state (all running and queued ones are always there)
HISTORY_PAGE = 60  # runs per /api/runs page
INBOX_SHOWN = 12
TRIAGE_ROWS = 30  # buckets in /api/board `triage`
CACHE_SECONDS = 30.0
ARCHIVE_SECONDS = 3600.0  # how often old runs are archived
GATE_CHECK_SECONDS = 60.0  # how often serve looks for a port build the regression corpus has not been gated on
STOP_GRACE = 3.0  # seconds a stopped run gets before its process group is killed
AUDIO_LEAD_SECONDS = 0.25  # a listener joins this far behind the live end of the sound
PAGE = Path(__file__).with_name("tas_page.html").read_text


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


class Server:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
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
        self.lanes_dir = profile.path("tas.serve.lanes", serve["lanes"]) if "lanes" in serve else None
        self.keep_days = float(serve.get("keep_days", 3))
        self.work = tas_board.Work(self.ranked, self.cfg["runs"] / "standing.json")
        self.triage = tas_board.Work(self.buckets, tas_findings.path(self.cfg["runs"]))
        self.oracles: dict[str, tuple[int, Any]] = {}  # run id -> (record mtime, summary) of finished runs
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.jobs))
        self.lock = threading.Lock()
        self.queued: dict[str, dict[str, Any]] = {}  # id -> placeholder row until the run writes its record
        self.cache: dict[Any, tuple[float, Any]] = {}
        self.gating = False

    # --- cached facts ---

    def _cached(self, key: Any, make: Any) -> Any:
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = make()
        self.cache[key] = (time.monotonic(), value)
        return value

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

        return self._cached(("movie", str(path), path.stat().st_mtime_ns), read)

    def ranked(self) -> list[dict[str, Any]]:
        """The ranked open-work list over the recorded standing (what `tas play --all` ended with)."""
        from abu_decomp import tas_health

        health = tas_health.read(self.cfg, self.profile)
        return tas.ranked_work(self.profile, self.cfg, health, tas.standing_rows(self.cfg))

    def buckets(self) -> list[dict[str, Any]]:
        """The findings feed grouped by root cause (module tas_buckets), the first TRIAGE_ROWS buckets."""
        rows = tas_buckets.build(tas_findings.read(self.cfg["runs"]), str(self.profile.source_file), None, TRIAGE_ROWS)
        tas_buckets.enrich(tas_triage._project(self.profile), rows)
        return rows

    def board(self) -> dict[str, Any]:
        project = tas_triage._project(self.profile)
        return {
            "decomp": self._cached("progress", lambda: tas_board.progress(self.states)),
            "landed": self._cached("landed", lambda: tas_board.landed(project)),
            "work": self.work.view(),
            "triage": self.triage.view(),
        }

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

    def corpus(self) -> dict[str, Any]:
        """The regression corpus, its problem spots and the last gate (module tas_corpus); {} before the first one."""

        def stamp(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        key = ("corpus", stamp(tas_corpus.corpus_path(self.cfg)), stamp(tas_corpus.gate_path(self.cfg)))
        try:
            return {**self._cached(key, lambda: tas_corpus.view(self.cfg)), "gating": self.gating}
        except DecompError as error:
            return {"error": str(error), "gating": self.gating}

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

    def row(self, run: Path, record: dict[str, Any]) -> dict[str, Any]:
        """One runs[] row (contract) of a run folder and its record."""
        running = record.get("status") == "running"
        taken = (run / tas.TAKEN).exists()
        return {
            "id": run.name,
            "movie": record.get("movie", ""),
            "status": "running" if running else "done",
            "speed": int(record.get("speed") or 0),
            "poll": tas.last_poll(run / tas.HASH) if running else int(record.get("polls_reached") or 0),
            "polls": int(record.get("polls") or 0),
            "outcome": None if running else record.get("outcome"),
            "subject": None if running else record.get("subject") or None,
            "started": record.get("started", 0),
            "ended": None if running else record.get("ended"),
            "label": record.get("label") or None,
            "port": record.get("port") or str(self.cfg["port"]),
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

    def state(self) -> dict[str, Any]:
        rows = tas.run_rows(self.cfg)
        on_disk = {row["id"] for _, row in rows if "id" in row}
        with self.lock:
            for run_id in [k for k in self.queued if k in on_disk]:
                del self.queued[run_id]
            queued = list(self.queued.values())
        live = [(run, row) for run, row in rows if row.get("status") == "running"]
        done = [(run, row) for run, row in rows if row.get("status") != "running"][-RECENT_SHOWN:]
        runs = [self.row(run, row) for run, row in live + done] + queued
        runs.sort(key=lambda r: r["started"])
        inbox = tas_board.inbox_lines(self.inbox)
        port = self.cfg["port"]
        try:
            built = port.stat().st_mtime
        except OSError:
            built = 0
        return {
            "now": time.time(),
            "build": {"port": str(port), "pin": self.pin_sha(), "built": built},
            "runs": runs,
            "movies": self.movies(rows, inbox),
            "lanes": tas_board.lanes(self.lanes_dir, runs),
            "inbox": inbox[-INBOX_SHOWN:],
        }

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

    def movies(self, rows: list[tuple[Path, dict[str, Any]]], inbox: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    def _forget(self, run_id: str) -> None:
        with self.lock:
            self.queued.pop(run_id, None)

    def human_running(self) -> str | None:
        """The run live input drives now, if any (one at a time)."""
        for run, row in tas.run_rows(self.cfg):
            if row.get("status") == "running" and (run / tas.TAKEN).exists():
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
        self.send(run, b"take")
        return {"id": run_id, "taken": True}

    def input(self, run_id: str, seq: int, buttons: int, x: int, y: int) -> dict[str, Any]:
        run = self.run_dir(run_id)
        if not run or not (run / tas.TAKEN).exists():
            raise DecompError("tas.input", f"run {run_id} is not driven by live input")
        if not (0 <= buttons <= 0xFFFF and -128 <= x <= 127 and -128 <= y <= 127 and seq >= 0):
            raise DecompError("tas.input", "buttons 0..0xffff, x and y -128..127, seq >= 0")
        self.send(run, f"{seq} {buttons:x} {x} {y}".encode())
        return {"ok": True}

    def send(self, run: Path, data: bytes) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(data, "\0" + tas.input_address(run)[1:])
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
            self.queued[run_id].update(input="human", took_at=len(point["polls"]), polls=len(point["polls"]))

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

    def points(self) -> dict[str, Any]:
        return tas_points.listing(self.profile)

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
            self.queued[run_id].update(input="human", took_at=take_at, polls=len(found["polls"]))
            self.queued[run_id].update({"from": source, "at": take_at})

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
        return tas.pin(self.cfg, run_id, keep)

    def archive(self) -> None:
        """Every ARCHIVE_SECONDS: delete runs past keep_days (tas.archive); a failure waits for the next round."""
        while True:
            with contextlib.suppress(OSError, DecompError):
                gone = tas.archive(self.cfg, self.keep_days)
                if gone:
                    print(f"tas page: archived {len(gone)} runs older than {self.keep_days:g} days", flush=True)
            time.sleep(ARCHIVE_SECONDS)

    def play_all(self) -> dict[str, Any]:
        folder = self.cfg["movies"]
        names = sorted(p.stem for p in folder.glob("*.tas")) if folder.is_dir() else []
        ids = {}
        with self.lock:
            for name in names:
                ids[name] = tas.new_run_id(self.cfg, name)
        for name, run_id in ids.items():
            self._placeholder(run_id, name, 0, self.cfg["port"], None)

        def go() -> None:
            try:
                tas.play_all(self.profile, jobs=self.jobs, run_ids=ids)
            except Exception:  # the runs' records say how each ended
                pass
            finally:
                for run_id in ids.values():
                    self._forget(run_id)

        threading.Thread(target=go, daemon=True).start()
        return {"ids": list(ids.values())}

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


def handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def reply(self, body: bytes, kind: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
                elif path == "/api/state":
                    self.json(server.state())
                elif path == "/api/coverage":
                    self.json(server.coverage())
                elif path == "/api/corpus":
                    self.json(server.corpus())
                elif path == "/api/points":
                    self.json(server.points())
                elif path == "/api/board":
                    self.json(server.board())
                elif path == "/api/runs":
                    args = dict(part.partition("=")[::2] for part in query.split("&") if part)
                    before = float(args["before"]) if args.get("before") else None
                    self.json(server.history(before, int(args.get("limit") or HISTORY_PAGE)))
                elif head == "coverage":
                    self.static(server.cfg["runs"] / tas_coverage.HTML, run_id or "index.html")
                elif head == "frame" and run_id:
                    self.frame(server.run_dir(run_id), query)
                elif head == "oracle" and run_id:
                    self.frame(server.run_dir(run_id), query, tas_oracle.VIEW)
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

        def static(self, root: Path, name: str) -> None:
            """A file of the gcovr HTML report (runs/coverage); a name that leaves the folder is a 404."""
            path = (root / name).resolve()
            if root.resolve() not in path.parents or not path.is_file():
                return self.missing()
            kind = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript"}
            self.reply(path.read_bytes(), kind.get(path.suffix, "application/octet-stream"))

        def frame(self, run: Path | None, query: str, name: str = tas.VIEW) -> None:
            after = None
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "after" and value.isdigit():
                    after = int(value)
            path = run / name if run else None
            stamp = 0
            if path and after is not None:  # long poll: the next picture, or 204 after a second
                deadline = time.monotonic() + 1.0
                while True:
                    with contextlib.suppress(OSError):
                        stamp = path.stat().st_mtime_ns // 1000
                    if stamp > after or time.monotonic() > deadline:
                        break
                    time.sleep(0.004)
                if stamp <= after:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
            try:
                with path.open("rb") as handle:  # type: ignore[union-attr]
                    stamp = os.fstat(handle.fileno()).st_mtime_ns // 1000
                    body = handle.read()
            except (OSError, AttributeError):
                body = b""
            if not body:
                return self.missing()
            seq = struct.unpack_from("<I", body, 6)[0] if len(body) >= 10 else 0
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/bmp")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame", str(stamp))
            self.send_header("X-Input-Seq", str(seq))
            self.send_header("Access-Control-Expose-Headers", "X-Frame, X-Input-Seq")
            self.end_headers()
            self.wfile.write(body)
            return None

        def shot(self, run: Path | None, query: str) -> None:
            """One side's picture at one compared poll, as a BMP: ?side=port|oracle&poll=N."""
            args = dict(part.partition("=")[::2] for part in query.split("&"))
            side, poll = args.get("side", ""), args.get("poll", "")
            if not run or side not in tas_oracle.SIDES or not poll.isdigit():
                return self.missing()
            pixels = tas_oracle.pixels(run / tas_oracle.SHOTS / side / f"poll-{int(poll):06d}.ppm")
            if pixels is None:
                return self.missing()
            return self.reply(tas_oracle.bmp(pixels), "image/bmp")

        def diff(self, run: Path | None) -> None:
            """Every compared poll's score: {"every", "rows": [{"poll", "diff", "drift", "vi_drift"}]}."""
            row = tas.read_record(run) if run else None
            meta = row.get("oracle") if row else None
            if not run or not isinstance(meta, dict):
                return self.missing()
            summary = tas_oracle.summary(run, row) or {}
            every = int(meta.get("every") or 1)
            rows = tas_oracle.compare(run, every, summary.get("status") == "done")
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
            # ?side=oracle: the original game's sound (audio-oracle.pcm), same layout and rate file as the port's
            name = tas_oracle.AUDIO if "side=oracle" in query.split("&") else tas.AUDIO
            pcm, rate_file = run / name, run / (name + ".rate")
            deadline = time.monotonic() + 10
            while not rate_file.is_file():  # the first sound has not been played yet
                if not tas.running(run) or time.monotonic() > deadline:
                    return self.missing()
                time.sleep(0.1)
            rate = int(rate_file.read_text().strip() or 32000)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(wav_header(rate))
            oracle = name == tas_oracle.AUDIO

            # The original replays faster than real time, so its file is played at wall pace from the run's own start
            # (what the port's listener hears at the same moment of the movie), not from the end of the file.
            def due() -> int:
                return int(max(0.0, time.time() - float(row.get("started") or 0)) * rate) * 4

            end = min(due(), pcm.stat().st_size) if oracle else pcm.stat().st_size
            offset = max(0, end - int(rate * AUDIO_LEAD_SECONDS) * 4) // 4 * 4
            with pcm.open("rb") as handle:
                while True:
                    handle.seek(offset)
                    chunk = handle.read()
                    if oracle:
                        chunk = chunk[: max(0, due() - offset)]
                    chunk = chunk[: len(chunk) // 4 * 4]
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        offset += len(chunk)
                        continue
                    if not tas.running(run):
                        return
                    time.sleep(0.05)

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
                elif self.path == "/api/play-all":
                    self.json(server.play_all())
                elif self.path == "/api/stop":
                    self.json(server.stop(str(body.get("id", ""))))
                elif self.path == "/api/pin":
                    self.json(server.keep(str(body.get("id", "")), bool(body.get("pinned", True))))
                elif self.path == "/api/take":
                    self.json(server.take(str(body.get("id", ""))))
                elif self.path == "/api/input":
                    self.json(
                        server.input(
                            str(body.get("id", "")),
                            int(body.get("seq", 0)),
                            int(body.get("buttons", 0)),
                            int(body.get("x", 0)),
                            int(body.get("y", 0)),
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
    threading.Thread(target=server.archive, daemon=True).start()
    threading.Thread(target=server.gatekeeper, daemon=True).start()
    print(f"tas page: http://{server.listen}/", flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return {"listen": server.listen}
