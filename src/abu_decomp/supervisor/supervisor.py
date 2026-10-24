"""Supervise a configured native process with an optional real GDB stop, headless."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp import procguard
from abu_decomp.files import atomic_json, sha256_file
from abu_decomp.inotify import ATTRIB, CREATE, DELETE, MOVED_TO, Watcher


def append_event(profile: Any, actor: str, status: str, message: str, evidence: str, payload: dict[str, Any]) -> None:
    """A RUN-FAIL through the one Cake Taste writer, to the seat feed; a seat without `events` records none."""
    from abu_decomp import events

    if profile.events_path is not None:
        record = {"actor": actor, "status": status, "message": message, "evidence": evidence, "payload": payload}
        events.append(profile.events_path, {**record, "kind": "RUN-FAIL"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fill(values: list[str], mapping: dict[str, str]) -> list[str]:
    return [str(value).format_map(mapping) for value in values]


class Supervisor:
    def __init__(self, profile: Any, role_id: str, config: dict[str, Any], screen=None):
        self.profile, self.role_id, self.config, self.screen = profile, role_id, config, screen
        self.on_change = lambda: None
        self.status_path = profile.resolve_path(config["status_path"])
        self.runs_root = profile.resolve_path(config["runs_root"])
        self.retry_delay = int(config["retry_delay_seconds"])
        self.watchdog = float(config["watchdog_seconds"])
        if self.retry_delay <= 0 or self.watchdog <= 0:
            raise ValueError("retry and watchdog durations must be positive")
        self.lines = deque(maxlen=int(config.get("max_lines", 2000)))
        self.phase = "starting"
        self.attempt = 0
        self.step = ""
        self.reason = ""
        self.stack = None
        self.capture = None
        self.last_result = ""
        self.last_exit = None
        self.signal_name = None
        self.hold_kind = None
        self.retry_remaining = 0
        self.paused = False
        self.proc = None
        self.restart = asyncio.Event()
        self.stack_ready = asyncio.Event()
        self.debug_stopped = False
        self.started_at = ""
        self.run_dir = None
        self.current_command: list[str] = []
        self.current_release = None
        self.current_binary = None
        self.current_sha = None
        self.last_announced_failure = None

    def status(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "role": self.role_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "step": self.step,
            "failure_reason": self.reason,
            "failure_kind": self.hold_kind,
            "debugger_stopped": self.debug_stopped,
            "stack": str(self.stack) if self.stack else None,
            "capture": str(self.capture) if self.capture else None,
            "last_result": self.last_result,
            "exit_code": self.last_exit,
            "retry_remaining_seconds": self.retry_remaining,
            "retry_delay_seconds": self.retry_delay,
            "paused": self.paused,
            "command": self.current_command,
            "release": str(self.current_release) if self.current_release else None,
            "binary_sha256": self.current_sha,
            "started_at": self.started_at,
            "terminal_rows_columns": list(self.screen.getmaxyx()) if self.screen else None,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "log": str(self.run_dir / "output.log") if self.run_dir else None,
            "updated_at": _now(),
        }

    def persist(self) -> None:
        atomic_json(self.status_path, self.status())
        self.on_change()

    def release_link(self) -> Path:
        """Resolve the parent, retaining the pin's name so promotions remain observable."""
        path = Path(self.config["release_current"]).expanduser()
        return self.profile.resolve_path(str(path.parent)) / path.name

    def _command(self, run_dir: Path) -> tuple[list[str], dict[str, str], Path]:
        link = self.release_link()
        release = link.resolve(strict=True)
        runtime = Path(self.profile.runtime_root).resolve()
        if not release.is_relative_to(runtime):
            raise ValueError("release resolves outside configured runtime root")
        self.current_release = release
        mapping = {"release": str(release), "run_dir": str(run_dir), "runtime_root": str(runtime)}
        argv = _fill(self.config["command"], mapping)
        binary = Path(argv[0]).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("configured native executable is not executable")
        environment = os.environ.copy()
        environment.update({key: str(value).format_map(mapping) for key, value in self.config.get("env", {}).items()})
        environment["XDG_CONFIG_HOME"] = str(run_dir / "config")
        environment["XDG_DATA_HOME"] = str(run_dir / "data")
        (run_dir / "config").mkdir()
        (run_dir / "data").mkdir()
        return argv, environment, binary

    def _debug_command(self, command: list[str], run_dir: Path) -> list[str]:
        debug = self.config.get("debugger")
        if not debug:
            return command
        return [
            sys.executable,
            "-m",
            "abu_decomp.supervisor.gdb",
            "--gdb",
            str(self.profile.resolve_path(debug["executable"])),
            "--library-dir",
            str(self.profile.resolve_path(debug["library_dir"])) if debug.get("library_dir") else "",
            "--breakpoint",
            str(debug["breakpoint"]),
            "--reason-expression",
            str(debug["reason_expression"]),
            "--run-dir",
            str(run_dir),
            "--",
            *command,
        ]

    async def _read_output(self, stream, output) -> None:
        while raw := await stream.readline():
            line = raw.decode(errors="replace").rstrip("\r\n")
            output.write(_now() + " " + line + "\n")
            output.flush()
            self.lines.append(line)
            for prefix in self.config.get("step_prefixes", []):
                if line.startswith(prefix):
                    self.step = line[len(prefix) :].strip()
            if line.startswith("debug-stop:"):
                self.hold_kind = "debugger"
                self.debug_stopped = True
                self.phase = "debugger_stopped"
            elif line.startswith("debug-reason:"):
                self.reason = line.split(":", 1)[1].strip()
            elif line.startswith("debug-stack:"):
                self.stack = Path(line.split(":", 1)[1].strip())
            elif line.startswith("debug-signal:"):
                self.signal_name = line.split(":", 1)[1].strip()
            elif line.startswith("C_STACK_END"):
                self.stack_ready.set()
            elif (complete_prefix := self.config.get("complete_prefix")) and line.startswith(complete_prefix):
                self.phase = "complete"
            self.persist()

    async def _countdown(self) -> bool:
        if not self.config.get("auto_retry", True):
            self.phase = "held_until_restart"
            self.persist()
            await self.restart.wait()
            return False
        self.retry_remaining = self.retry_delay
        self.phase = "debugger_hold_countdown" if self.debug_stopped else "retry_countdown"
        self.persist()
        while self.retry_remaining:
            if self.restart.is_set():
                return False
            await asyncio.sleep(1)
            if not self.paused:
                self.retry_remaining -= 1
                self.persist()
        return True

    async def _stop_child(self) -> None:
        if self.proc and self.proc.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except TimeoutError:
                os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()

    async def _capture(self, run_dir: Path) -> None:
        configured = self.config.get("capture_command")
        if not configured:
            return
        target = run_dir / "failure.png"
        command = _fill(configured, {"capture": str(target), "run_dir": str(run_dir)})
        try:
            process = await asyncio.create_subprocess_exec(*command)
            code = await asyncio.wait_for(process.wait(), timeout=10)
            if code == 0 and target.is_file() and target.stat().st_size:
                self.capture = target
        except (TimeoutError, OSError) as error:
            self.lines.append("[capture] " + str(error))
        self.persist()

    async def run_once(self) -> bool:
        self.attempt += 1
        self.restart.clear()
        self.stack_ready.clear()
        self.step = self.reason = ""
        self.stack = self.capture = None
        self.signal_name = self.hold_kind = None
        self.debug_stopped = False
        self.retry_remaining = 0
        self.started_at = _now()
        self.proc = None
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + str(self.attempt)
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True)
        self.run_dir = run_dir
        self.lines.append("=== attempt " + str(self.attempt) + " ===")
        command, environment, binary = self._command(run_dir)
        self.current_binary = binary
        self.current_sha = sha256_file(binary)
        self.current_command = command
        launched = self._debug_command(command, run_dir)
        self.phase = "starting"
        self.persist()
        exit_code = 127
        outcome = "launch_failed"
        with (run_dir / "output.log").open("w", buffering=1) as output:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *launched,
                    cwd=run_dir,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **procguard.owned(),
                )
            except OSError as error:
                self.lines.append("[launch] " + str(error))
                output.write("launch failed: " + str(error) + "\n")
            else:
                assert self.proc.stdout is not None
                reader = asyncio.create_task(self._read_output(self.proc.stdout, output))
                wait = asyncio.create_task(self.proc.wait())
                stack = asyncio.create_task(self.stack_ready.wait())
                manual = asyncio.create_task(self.restart.wait())
                try:
                    done, _ = await asyncio.wait(
                        {wait, stack, manual},
                        timeout=self.watchdog,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if manual in done:
                        outcome = "manual_restart"
                        await self._stop_child()
                    elif stack in done and self.debug_stopped:
                        await self._capture(run_dir)
                        if await self._countdown() and self.proc.returncode is None:
                            self.phase = "fatal_continuing"
                            self.persist()
                            assert self.proc.stdin is not None
                            self.proc.stdin.write(b"continue\n")
                            await self.proc.stdin.drain()
                            exit_code = await self.proc.wait()
                            outcome = "debugger_exit"
                        else:
                            outcome = "manual_restart"
                            await self._stop_child()
                    elif wait in done or stack in done:
                        exit_code = await asyncio.wait_for(wait, timeout=5)
                        outcome = "clean_exit" if exit_code == 0 else "nonzero_exit"
                    else:
                        outcome = "watchdog_timeout"
                        await self._stop_child()
                    await reader
                finally:
                    for task in (wait, stack, manual):
                        if not task.done():
                            task.cancel()
                    if not reader.done():  # never leave it writing to the log the `with` is about to close
                        reader.cancel()
                        with suppress(asyncio.CancelledError):
                            await reader
        if self.proc and self.proc.returncode is not None:
            exit_code = self.proc.returncode
        if self.signal_name and outcome == "nonzero_exit":
            outcome = "signal_exit"
        self.last_exit = exit_code
        self.last_result = outcome
        self.phase = "failed" if outcome not in {"clean_exit", "manual_restart"} else outcome
        self.debug_stopped = False
        self.persist()
        receipt = {
            **self.status(),
            "outcome": outcome,
            "ended_at": _now(),
            "run_dir": str(run_dir),
            "log": str(run_dir / "output.log"),
            "repro_command": shlex.join(command),
            "stack": str(self.stack) if self.stack else None,
            "capture": str(self.capture) if self.capture else None,
        }
        receipt_path = run_dir / "receipt.json"
        atomic_json(receipt_path, receipt)
        failure = (outcome, self.reason, self.signal_name)
        if outcome not in {"clean_exit", "manual_restart"} and failure != self.last_announced_failure:
            append_event(
                self.profile,
                self.role_id,
                "blocked" if self.hold_kind == "debugger" else "failed",
                self.reason or outcome,
                str(receipt_path),
                {"outcome": outcome, "exit_code": exit_code, "signal": self.signal_name, "attempt": self.attempt},
            )
            self.last_announced_failure = failure
        if outcome == "manual_restart":
            return True
        if outcome == "debugger_exit":
            return True
        return await self._countdown()

    def release_changed(self, watcher: Watcher) -> None:
        """A promoted pin wakes even a debugger-held run; unrelated directory writes do nothing."""
        link = self.release_link()
        if (
            any(path == link for path, _mask in watcher.read())
            and self.current_release is not None
            and link.resolve() != self.current_release
        ):
            self.restart.set()

    async def manage(self) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        link = self.release_link()
        watcher = Watcher()
        loop = asyncio.get_running_loop()
        try:
            watcher.add(link.parent, CREATE | MOVED_TO | DELETE | ATTRIB)
            loop.add_reader(watcher.fd, self.release_changed, watcher)
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    self.phase = "supervisor_error"
                    self.reason = str(error)
                    self.lines.append("[supervisor] " + str(error))
                    self.persist()
                    await self._countdown()
        finally:
            loop.remove_reader(watcher.fd)
            watcher.close()


def pidfile(status_path: Path) -> Path:
    """Where the headless supervisor publishes its pid, so a tool can send it SIGHUP and SIGUSR1."""
    return status_path.with_name(status_path.name + ".pid")


def run_supervisor(profile: Any, role_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Run the role headless until SIGTERM or SIGINT. SIGHUP restarts the run now; SIGUSR1 toggles the retry pause.

    This process draws nothing; the status file is `status_path`. Its pid is in
    `pidfile(status_path)` while it runs.
    """
    supervisor = Supervisor(profile, role_id, config)
    published = pidfile(supervisor.status_path)
    published.parent.mkdir(parents=True, exist_ok=True)
    temporary = published.with_name(published.name + ".new")
    temporary.write_text(f"{os.getpid()}\n")
    os.replace(temporary, published)

    def restart() -> None:
        supervisor.paused = False
        supervisor.restart.set()

    def pause() -> None:
        supervisor.paused = not supervisor.paused
        supervisor.persist()

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGHUP, restart)
        loop.add_signal_handler(signal.SIGUSR1, pause)
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, stop.set)
        task = asyncio.create_task(supervisor.manage())
        ended = asyncio.create_task(stop.wait())
        await asyncio.wait({task, ended}, return_when=asyncio.FIRST_COMPLETED)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await supervisor._stop_child()

    try:
        asyncio.run(serve())
    finally:
        with suppress(FileNotFoundError):
            if published.read_text().strip() == str(os.getpid()):
                published.unlink()
    return {"closed": True, "role": role_id, "status_path": str(supervisor.status_path)}
