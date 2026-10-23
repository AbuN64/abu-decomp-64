"""`decomp tas serve`: the TAS page and the API behind it (orch plans/TAS-PAGE-CONTRACT.md).

Section `tas.serve`: {"listen": "HOST:PORT", "jobs": N, "inbox": path, "states": path, "pin": path, "lanes": path}.
Every run (started here, by `tas play` in a shell, or by `play --all`) is a folder under `runs/plays` whose
`run.json` the port's run writes (module tas); the page sees all of them: the port's own latest picture
(`view.bmp`), its log and its sound (`audio.pcm`, served as a growing WAV). Any running run can be taken over by
live input (`/api/take`, `/api/input`: datagrams to the run's input socket, tas.input_address), and `/api/play`
without a movie starts a human run; one run is driven by live input at a time. `inbox` is the integration train's
inbox (lines marked `| LANDED` / `| DROPPED`), `states` the decomp progress states (its `none_fns`), `pin` the git
folder of the decomp source the port was built from, `lanes` the folder of cycle lane dirs mirrored from the
orchestrator (brief.md, STATUS.md, RESULT.md, cycle.json each), shown as the cycle panel. Function state (exact /
fuzzy / none) is read from the decomp source, as the progress states define it: a NON_MATCHING body is fuzzy,
INCLUDE_ASM alone is none.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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

from abu_decomp import DecompError, evidence, tas, tas_triage
from abu_decomp.config import Profile

_SERVE_KEYS = frozenset({"listen", "jobs", "inbox", "states", "pin", "lanes"})
RUNS_SHOWN = 24  # newest runs in `runs` (every running one is always there)
INBOX_SHOWN = 12
CACHE_SECONDS = 30.0
MARK = re.compile(r"\|\s*(LANDED|DROPPED|DRYRUN-OK)\b")
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


def _first_line(path: Path) -> str | None:
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return line.strip()
    except OSError:
        return None
    return None


def lane_row(lane: Path) -> dict[str, Any] | None:
    """One cycle lane dir (cycle.json or a brief whose first line is `KIND: cycle`) as a cycle-panel row, else None."""
    try:
        cycle = json.loads((lane / "cycle.json").read_text())
    except (OSError, ValueError):
        cycle = None
    try:
        brief = (lane / "brief.md").read_text(errors="replace")
    except OSError:
        brief = ""
    if not isinstance(cycle, dict) and not brief.lstrip().startswith("KIND: cycle"):
        return None
    cycle = cycle if isinstance(cycle, dict) else {}
    title = cycle.get("title") or next((ln[2:].strip() for ln in brief.splitlines() if ln.startswith("# ")), lane.name)
    status, status_at = None, None
    try:
        with (lane / "STATUS.md").open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 4096))
            lines = [ln.strip() for ln in handle.read().decode(errors="replace").splitlines() if ln.strip()]
        status = lines[-1] if lines else None
        status_at = (lane / "STATUS.md").stat().st_mtime
    except OSError:
        pass
    result = _first_line(lane / "RESULT.md")
    return {"name": lane.name, "title": str(title), "status": status, "status_at": status_at, "result": result}


def inbox_lines(path: Path | None) -> list[dict[str, Any]]:
    """Each inbox line without its mark, and the mark (LANDED / DROPPED / DRYRUN-OK) or None while unlanded."""
    try:
        text = path.read_text(errors="replace") if path else ""
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = MARK.search(line)
        rows.append({"line": line[: match.start()].strip() if match else line, "mark": match[1] if match else None})
    return rows


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
        self.pool = ThreadPoolExecutor(max_workers=max(1, self.jobs))
        self.lock = threading.Lock()
        self.queued: dict[str, dict[str, Any]] = {}  # id -> placeholder row until the run writes its record
        self.cache: dict[Any, tuple[float, Any]] = {}

    # --- cached facts ---

    def _cached(self, key: Any, make: Any) -> Any:
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = make()
        self.cache[key] = (time.monotonic(), value)
        return value

    def pin(self) -> str | None:
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
                if name in (version.get("none_fns") or []):
                    return "none"
            project = tas_triage._project(self.profile)
            site = tas_triage.definition_site(project, name) if project else None
            if site and project:
                fn = evidence.parse((project / site.rpartition(":")[0]).read_text(errors="replace")).get(name)
                return "fuzzy" if fn is not None and fn.kind == evidence.NM else "exact"
            return "none"

        return self._cached(("fn", name), read)

    def movie_polls(self, path: Path) -> int:
        def read() -> int:
            try:
                return len(tas.decode(path.read_text())[1])
            except (OSError, DecompError, ValueError):
                return 0

        return self._cached(("polls", str(path), path.stat().st_mtime_ns), read)

    # --- the contract ---

    def state(self) -> dict[str, Any]:
        rows = tas.run_rows(self.cfg)
        on_disk = {row["id"] for _, row in rows if "id" in row}
        with self.lock:
            for run_id in [k for k in self.queued if k in on_disk]:
                del self.queued[run_id]
            queued = list(self.queued.values())
        runs = []
        for run, row in rows:
            running = row.get("status") == "running"
            runs.append(
                {
                    "id": run.name,
                    "movie": row.get("movie", ""),
                    "status": "running" if running else "done",
                    "speed": int(row.get("speed") or 0),
                    "poll": tas.last_poll(run / tas.HASH) if running else int(row.get("polls_reached") or 0),
                    "polls": int(row.get("polls") or 0),
                    "outcome": None if running else row.get("outcome"),
                    "subject": None if running else row.get("subject") or None,
                    "started": row.get("started", 0),
                    "frame": f"/frame/{run.name}",
                    "label": row.get("label") or None,
                    "port": row.get("port") or str(self.cfg["port"]),
                    "input": "human" if (run / tas.TAKEN).exists() else "movie",
                    "saved": row.get("saved") or None,
                    "pak": row.get("pak") or "on",
                }
            )
        shown = [r for r in runs if r["status"] == "running"] + queued
        shown += [r for r in runs if r["status"] != "running"][-max(0, RUNS_SHOWN - len(shown)) :]
        shown.sort(key=lambda r: r["started"])
        inbox = inbox_lines(self.inbox)
        movies = self.movies(rows, inbox)
        port = self.cfg["port"]
        try:
            built = port.stat().st_mtime
        except OSError:
            built = 0
        return {
            "now": time.time(),
            "build": {"port": str(port), "pin": self.pin(), "built": built},
            "runs": shown,
            "movies": movies,
            "cycle": {"lanes": self.lanes(shown), "inbox": inbox[-INBOX_SHOWN:]},
        }

    def movies(self, rows: list[tuple[Path, dict[str, Any]]], inbox: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            standing = json.loads((self.cfg["runs"] / "standing.json").read_text())
        except (OSError, ValueError):
            standing = {}
        latest: dict[str, dict[str, Any]] = {}
        for _, row in rows:
            if row.get("status") == "done" and row.get("outcome") and not row.get("stopped") and not row.get("taken"):
                latest[row.get("movie", "")] = row
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
            out.append(
                {
                    "name": name,
                    "polls": self.movie_polls(path),
                    "outcome": known.get("outcome") or "",
                    "subject": known.get("subject") or None,
                    "reached": int(known.get("polls_reached") or 0),
                    "same_twice": last.get("same_twice") if last else None,
                    "fn": {"name": fn, "state": self.fn_state(fn)} if fn else None,
                    "train": train,
                    "last_run": last.get("ended") if last else None,
                    "label": last.get("label") if last else None,
                    "kind": known.get("kind") or None,
                }
            )
        return out

    def lanes(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The cycle lanes in the mirrored lanes folder: active ones (no RESULT.md yet) first, newest status first."""
        folder = self.lanes_dir
        out = []
        for lane in sorted(folder.iterdir()) if folder and folder.is_dir() else []:
            row = lane_row(lane)
            if row:
                row["runs"] = sum(1 for r in runs if r["status"] == "running" and r.get("label") == lane.name)
                out.append(row)
        out.sort(key=lambda r: (r["result"] is not None, -(r["status_at"] or 0), r["name"]))
        return out

    def _placeholder(
        self, run_id: str, movie: str, speed: int, port: Path, label: str | None, pak: str | None = None
    ) -> None:
        polls = 0
        with contextlib.suppress(OSError):
            polls = self.movie_polls(self.cfg["movies"] / f"{movie}.tas")
        if pak is None:  # a replay keeps what its movie ran with
            with contextlib.suppress(DecompError, OSError):
                pak = tas.movie_pak(self.cfg, movie)
        row = {"id": run_id, "movie": movie, "status": "running", "speed": speed, "poll": 0, "polls": polls}
        row.update(outcome=None, subject=None, started=time.time(), frame=f"/frame/{run_id}")
        row.update(label=label or None, port=str(port), input="movie", saved=None, pak=pak or "on")
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

    def new_human(self, port: str | None, label: str | None, pak: str | None = None) -> dict[str, Any]:
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        tas.check_pak(pak)
        exe = tas.with_port(self.cfg, port)["port"]
        movie = tas.human_movie_name()
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
        self._placeholder(run_id, movie, 1, exe, label or tas.HUMAN_LABEL, pak or "on")
        with self.lock:
            self.queued[run_id]["input"] = "human"

        def go() -> None:
            try:
                tas.play(self.profile, None, speed=1, run_id=run_id, port=port or None, label=label or None, pak=pak)
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
        self, movie: str, speed: int, port: str | None = None, label: str | None = None, pak: str | None = None
    ) -> dict[str, Any]:
        if not movie:
            return self.new_human(port, label, pak)
        tas.movie_polls(self.cfg, movie)  # refuse a missing movie or another ROM here, not in the thread
        if not 0 <= speed <= 1000:
            raise DecompError("tas.play", f"speed is 0 (turbo) .. 1000, not {speed}")
        tas.check_label(label)
        tas.check_pak(pak)
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
            while run_id in self.queued:
                run_id += "-q"
        self._placeholder(run_id, movie, speed, exe, label, pak)

        def go() -> None:
            try:
                tas.play(
                    self.profile, movie, speed=speed, run_id=run_id, port=port or None, label=label or None, pak=pak
                )
            except Exception:  # the record says done; the page shows it
                pass
            finally:
                self._forget(run_id)

        self.pool.submit(go)
        return {"id": run_id}

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
        if not re.fullmatch(r"[\w.-]+", run_id or "") or run_id.startswith("."):
            return None
        run = self.cfg["runs"] / "plays" / run_id
        return run if run.is_dir() else None

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
                elif head == "frame" and run_id:
                    self.frame(server.run_dir(run_id), query)
                elif head == "log" and run_id:
                    self.log(server.run_dir(run_id), query)
                elif head == "audio" and run_id:
                    self.audio(server.run_dir(run_id))
                else:
                    self.missing()
            except (BrokenPipeError, ConnectionResetError):
                return

        def frame(self, run: Path | None, query: str) -> None:
            after = None
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "after" and value.isdigit():
                    after = int(value)
            path = run / tas.VIEW if run else None
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

        def audio(self, run: Path | None) -> None:
            row = tas.read_record(run) if run else None
            if not run or not row:
                return self.missing()
            pcm, rate_file = run / tas.AUDIO, run / (tas.AUDIO + ".rate")
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
            offset = max(0, pcm.stat().st_size - int(rate * AUDIO_LEAD_SECONDS) * 4) // 4 * 4
            with pcm.open("rb") as handle:
                while True:
                    handle.seek(offset)
                    chunk = handle.read()
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
                        )
                    )
                elif self.path == "/api/play-all":
                    self.json(server.play_all())
                elif self.path == "/api/stop":
                    self.json(server.stop(str(body.get("id", ""))))
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
    print(f"tas page: http://{server.listen}/", flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return {"listen": server.listen}
