"""Event-driven process supervision: pidfd readiness, restart delays, hold-the-screen loops."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Unit:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    restart_seconds: float | None = None  # None: runs once and is never restarted
    fatal: bool = False  # exit of this unit ends the whole group (the compositor)
    process: subprocess.Popen | None = None
    pidfd: int | None = None
    restart_at: float | None = None
    starts: int = 0


class FatalUnitExit(RuntimeError):
    def __init__(self, name: str, code: int):
        super().__init__(f"unit {name} exited with {code}")
        self.name, self.code = name, code


class Group:
    """Runs units, restarts those with a delay, and raises when a fatal unit exits."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.monotonic):
        self.units: dict[str, Unit] = {}
        self.selector = selectors.DefaultSelector()
        self._popen, self._clock = popen, clock
        self._handlers: dict[int, Callable[[], None]] = {}
        self.on_spawn: Callable[[Unit], None] | None = None  # called after every (re)start, with unit.process set

    def watch(self, fd: int, handler: Callable[[], None]) -> None:
        """Call handler whenever fd is readable, inside the same event loop as process exits."""
        self._handlers[fd] = handler
        self.selector.register(fd, selectors.EVENT_READ, fd)

    def start(self, unit: Unit) -> None:
        self.units[unit.name] = unit
        self._spawn(unit)

    def _spawn(self, unit: Unit) -> None:
        env = {**os.environ, **unit.env}
        unit.process = self._popen(unit.command, env=env, start_new_session=True)
        unit.starts += 1
        unit.restart_at = None
        if self.on_spawn:
            self.on_spawn(unit)
        unit.pidfd = os.pidfd_open(unit.process.pid)
        self.selector.register(unit.pidfd, selectors.EVENT_READ, unit.name)

    def _reap(self, unit: Unit) -> int:
        assert unit.process is not None and unit.pidfd is not None
        self.selector.unregister(unit.pidfd)
        os.close(unit.pidfd)
        unit.pidfd = None
        code = unit.process.wait()
        unit.process = None
        return code

    def step(self, max_wait: float | None = None) -> list[str]:
        """Handle one round of events; return names of units that exited. Raises on fatal exit."""
        now = self._clock()
        due = [u.restart_at for u in self.units.values() if u.restart_at is not None]
        timeout = max(0.0, min(due) - now) if due else max_wait
        if due and max_wait is not None:
            timeout = min(timeout, max_wait)
        exited: list[str] = []
        for key, _ in self.selector.select(timeout):
            if isinstance(key.data, int):
                try:
                    self._handlers[key.data]()
                except ConnectionError:
                    self.selector.unregister(key.data)
                continue
            unit = self.units[key.data]
            code = self._reap(unit)
            exited.append(unit.name)
            if unit.fatal:
                raise FatalUnitExit(unit.name, code)
            if unit.restart_seconds is not None:
                unit.restart_at = self._clock() + unit.restart_seconds
        now = self._clock()
        for unit in self.units.values():
            if unit.restart_at is not None and unit.restart_at <= now:
                self._spawn(unit)
        return exited

    def run_forever(self) -> None:
        while True:
            self.step()

    def stop(self) -> None:
        for unit in self.units.values():
            if unit.process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(unit.process.pid, signal.SIGTERM)
        deadline = self._clock() + 5
        for unit in self.units.values():
            if unit.process is None:
                continue
            try:
                unit.process.wait(timeout=max(0.1, deadline - self._clock()))
            except subprocess.TimeoutExpired:
                os.killpg(unit.process.pid, signal.SIGKILL)
                unit.process.wait()
            if unit.pidfd is not None:
                self.selector.unregister(unit.pidfd)
                os.close(unit.pidfd)
                unit.pidfd = None
            unit.process = None


class RoleRunner:
    """Runs one role command once and holds its last output; restart and stop arrive as signals.

    SIGUSR1 (re)starts the command, SIGUSR2 stops it. Nothing restarts the command on its own, and nothing
    is cleared between runs, so the final screen of a stopped role stays visible.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        title: str,
        out: Any = None,
        popen: Callable[..., Any] = subprocess.Popen,
        on_start: Callable[[], None] | None = None,
    ):
        self.command, self.title = list(command), title
        self.out = out or sys.stdout
        self._popen, self.on_start = popen, on_start
        self.child: Any = None
        self.starts = 0

    def _say(self, text: str) -> None:
        print(f"\n[{time.strftime('%H:%M:%S')}] {self.title} {text}", file=self.out, flush=True)

    def start(self) -> None:
        self.child = self._popen(self.command, start_new_session=True)
        self.starts += 1
        if self.on_start:
            self.on_start()

    def terminate(self, grace: float = 5.0) -> None:
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def restart(self) -> None:
        self.terminate()
        self._say("restarting on request")
        self.start()

    def stop(self) -> None:
        if self.child is not None and self.child.poll() is None:
            self.terminate()
            self._say("stopped on request; `decomp stream restart` runs it again")

    def reap(self) -> bool:
        """Report a child that exited by itself; True when one did."""
        if self.child is not None and self.child.poll() is not None:
            code = self.child.returncode
            self.child = None
            self._say(f"exited rc={code}; held, `decomp stream restart` runs it again")
            return True
        return False

    def serve(self) -> None:
        """Run once, then block on child exit and control signals; no timers, no polling."""
        wake_read, wake_write = os.pipe()
        os.set_blocking(wake_write, False)
        previous = signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
        for number in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM):
            signal.signal(number, lambda *_: None)
        selector = selectors.DefaultSelector()
        selector.register(wake_read, selectors.EVENT_READ, "signal")
        self.start()
        watched = None
        try:
            while True:
                if self.child is not None and watched != self.child.pid:
                    watched = self.child.pid
                    selector.register(os.pidfd_open(watched), selectors.EVENT_READ, "child")
                for key, _ in selector.select():
                    if key.data == "child":
                        with contextlib.suppress(KeyError, ValueError):
                            selector.unregister(key.fd)
                            os.close(key.fd)
                        watched = None
                        self.reap()
                        continue
                    for number in os.read(wake_read, 64):
                        if number == signal.SIGUSR1:
                            self.restart()
                            if watched is not None:
                                self._drop_watch(selector)
                                watched = None
                        elif number == signal.SIGUSR2:
                            self.stop()
                            self._drop_watch(selector)
                            watched = None
                        elif number == signal.SIGTERM:
                            self.terminate()
                            return
        finally:
            signal.set_wakeup_fd(previous)

    @staticmethod
    def _drop_watch(selector: Any) -> None:
        for key in list(selector.get_map().values()):
            if key.data == "child":
                selector.unregister(key.fd)
                os.close(key.fd)
