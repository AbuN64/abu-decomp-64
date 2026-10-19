"""One rich, expandable console for mission evidence and supervised boot chains."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, Footer, Static

from .dashboard_model import Dashboard, Row
from .dashboard_sources import Sources
from .feed import canonical_kind
from .terminal_driver import ConsoleDriver

WATCH_MASK = 0x00000002 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200


def _watch_directory(directory: Path) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_init1.restype = ctypes.c_int
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.inotify_add_watch.restype = ctypes.c_int
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    if libc.inotify_add_watch(fd, os.fsencode(directory), WATCH_MASK) < 0:
        code = ctypes.get_errno()
        os.close(fd)
        raise OSError(code, "inotify_add_watch")
    return fd


def selected_events(records, kinds: set[str] | None):
    return [record for record in records if kinds is None or canonical_kind(record) in kinds]


class EvidenceRow(Collapsible):
    def __init__(self, row: Row):
        self.record = row
        self.body = Static(Text(row.details), classes="details")
        super().__init__(
            self.body,
            title=self.label(row),
            collapsed=not row.open,
            id="row-" + hashlib.sha256(row.key.encode()).hexdigest()[:16],
            classes=row.state,
        )

    @staticmethod
    def label(row: Row) -> str:
        marker = {"passed": "✓", "stop": "!", "planned": "○", "raw": "≡"}.get(row.state, "•")
        # Collapsible titles accept markup, so escape untrusted evidence.
        return Text(f"{marker} {row.title}").markup

    def update_record(self, row: Row) -> None:
        if row != self.record:
            self.remove_class(self.record.state)
            self.add_class(row.state)
            self.record = row
            self.title = self.label(row)
            self.body.update(Text(row.details))


class ConsoleApp(App):
    AUTO_FOCUS = None
    CSS = """
    Screen { background: #101820; color: #d8e2eb; }
    #banner { dock: top; height: auto; max-height: 5; }
    #heading { height: auto; max-height: 2; background: #213346;
               color: #ffffff; text-style: bold; padding: 0 1; }
    #summary { height: auto; max-height: 3; color: #b6d6e6; padding: 0 1; }
    #rows { padding: 0 1; }
    #foot { height: auto; max-height: 2; background: #213346; color: #b6d6e6; padding: 0 1; }
    EvidenceRow { border: none; padding: 0; margin: 0; height: auto; }
    CollapsibleTitle { height: 1; padding: 0; text-wrap: nowrap; text-overflow: ellipsis; }
    CollapsibleTitle:focus { background: #304a5d; text-style: bold; }
    .passed > CollapsibleTitle { color: #88d5a0; }
    .stop > CollapsibleTitle { color: #ffcd82; background: #493527; text-style: bold; }
    .planned > CollapsibleTitle { color: #7c8997; }
    .raw > CollapsibleTitle { color: #8a9eb0; }
    .details { height: auto; padding: 0 1 1 2; color: #c0d1de; background: #18242e; }
    Footer { background: #213346; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("j,down", "next_row", "Next", show=False),
        Binding("k,up", "previous_row", "Previous", show=False),
        Binding("space", "toggle_row", "Expand"),
        Binding("home", "first_row", "First"),
        Binding("end", "last_row", "Last"),
        Binding("f5", "restart", "Restart", show=False),
        Binding("f7", "pause", "Pause retry", show=False),
    ]

    def __init__(self, sources: Sources, supervisor=None, signals: SupervisorSignals | None = None):
        super().__init__(driver_class=ConsoleDriver)
        self.sources, self.supervisor, self.signals = sources, supervisor, signals
        self.widgets: dict[str, EvidenceRow] = {}
        self.watchers: dict[Path, int] = {}
        self.refresh_pending = False
        self.initial = True
        self.manage_task = None
        self.model: Dashboard | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="banner"):
            yield Static("Loading evidence…", id="heading")
            yield Static("", id="summary")
        yield VerticalScroll(id="rows")
        yield Static("", id="foot")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_sources()
        if self.supervisor:
            self.supervisor.on_change = self.source_changed
            self.manage_task = asyncio.create_task(self.supervisor.manage())

    def source_changed(self) -> None:
        if not self.refresh_pending:
            self.refresh_pending = True
            self.set_timer(0.15, self.refresh_sources)

    def file_ready(self, fd: int) -> None:
        with suppress(BlockingIOError):
            os.read(fd, 65536)
        self.source_changed()

    async def refresh_sources(self) -> None:
        self.refresh_pending = False
        model = self.sources.read()
        self.model = model
        self.query_one("#heading", Static).update(Text(model.heading))
        self.query_one("#summary", Static).update(Text(model.summary))
        self.query_one("#foot", Static).update(Text(model.footer))
        container = self.query_one("#rows", VerticalScroll)
        keys = [row.key for row in model.rows]
        for key in list(self.widgets):
            if key not in keys:
                await self.widgets.pop(key).remove()
        for row in model.rows:
            if row.key in self.widgets:
                self.widgets[row.key].update_record(row)
            else:
                widget = EvidenceRow(row)
                self.widgets[row.key] = widget
                await container.mount(widget)
        container.sort_children(key=lambda widget: keys.index(widget.record.key))
        if self.initial and model.focus_key in self.widgets:
            widget = self.widgets[model.focus_key]
            widget.query_one("CollapsibleTitle").focus()
            self.call_after_refresh(container.scroll_to, y=max(0, keys.index(model.focus_key) - 7), animate=False)
            self.initial = False
        loop = asyncio.get_running_loop()
        directories = self.sources.directories()
        for directory in self.watchers.keys() - directories:
            fd = self.watchers.pop(directory)
            loop.remove_reader(fd)
            os.close(fd)
        for directory in directories - self.watchers.keys():
            fd = _watch_directory(directory)
            self.watchers[directory] = fd
            loop.add_reader(fd, self.file_ready, fd)

    def _titles(self):
        return list(self.query("CollapsibleTitle"))

    def _move(self, delta: int) -> None:
        titles = self._titles()
        if titles:
            current = titles.index(self.focused) if self.focused in titles else 0
            titles[max(0, min(len(titles) - 1, current + delta))].focus()

    def action_next_row(self) -> None:
        self._move(1)

    def action_previous_row(self) -> None:
        self._move(-1)

    def action_first_row(self) -> None:
        if titles := self._titles():
            titles[0].focus()

    def action_last_row(self) -> None:
        if titles := self._titles():
            titles[-1].focus()

    def action_toggle_row(self) -> None:
        if self.focused and isinstance(self.focused.parent, Collapsible):
            row = self.focused.parent
            row.collapsed = not row.collapsed

    def action_restart(self) -> None:
        if self.supervisor:
            self.supervisor.paused = False
            self.supervisor.restart.set()
        elif self.signals:
            self.notify(self.signals.send(signal.SIGHUP, "restart"))

    def action_pause(self) -> None:
        if self.supervisor:
            self.supervisor.paused = not self.supervisor.paused
            self.supervisor.persist()
        elif self.signals:
            self.notify(self.signals.send(signal.SIGUSR1, "pause toggle"))

    async def on_unmount(self) -> None:
        loop = asyncio.get_running_loop()
        for fd in self.watchers.values():
            loop.remove_reader(fd)
            os.close(fd)
        if self.manage_task:
            self.manage_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.manage_task
            await self.supervisor._stop_child()


class SupervisorSignals:
    """F5 and F7 for a headless `decomp supervisor run`: SIGHUP restarts the run, SIGUSR1 toggles the retry pause."""

    def __init__(self, pidfile: Path) -> None:
        self.pidfile = pidfile

    def send(self, number: int, action: str) -> str:
        try:
            pid = int(self.pidfile.read_text())
            os.kill(pid, number)
        except (OSError, ValueError) as error:
            return f"supervisor {action} not sent: {error}"
        return f"supervisor {action} sent to {pid}"


def run_console(
    path: Path,
    *,
    title: str,
    max_lines: int = 200,
    kinds: set[str] | None = None,
    host_log: Path | None = None,
    sources: dict | None = None,
) -> None:
    status = (sources or {}).get("supervisor_status")
    signals = None
    if status:
        from abu_decomp.supervisor.supervisor import pidfile

        signals = SupervisorSignals(pidfile(Path(status)))
    ConsoleApp(
        Sources(path, title=title, config=sources, kinds=kinds, host_log=host_log, limit=min(max_lines, 200)),
        signals=signals,
    ).run()
