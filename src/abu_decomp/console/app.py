"""One rich, expandable console for mission evidence and supervised boot chains."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Collapsible, Static

from .dashboard_model import Dashboard, Row, ago
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
    def label(row: Row, now: float | None = None) -> str:
        """Dim fixed-width age column, then the state marker and the title (Collapsible titles take markup)."""
        marker = {"passed": "✓", "cracked": "★", "stop": "!", "planned": "○", "raw": "≡"}.get(row.state, "•")
        stamp = ago((now or time.time()) - row.when) if row.when else ""
        tag = f"  [dim]{escape(row.tag)}[/dim]" if row.tag else ""
        return f"[dim]{stamp:>4}[/dim] " + escape(f"{marker} {row.title}") + tag

    def update_record(self, row: Row) -> None:
        if row != self.record:
            self.remove_class(self.record.state)
            self.add_class(row.state)
            self.record = row
            self.title = self.label(row)
            self.body.update(Text(row.details))

    def tick(self, now: float) -> None:
        if self.record.when:
            self.title = self.label(self.record, now)


class FollowScroll(VerticalScroll):
    """Follows new content at the bottom until the user scrolls up; `follow()` (End, the latest control) resumes."""

    following = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.following = new_value >= self.max_scroll_y - 1
        self.app.query_one("#latest").display = not self.following

    def follow(self) -> None:
        self.following = True
        self.scroll_end(animate=False)
        self.app.query_one("#latest").display = False


class LatestControl(Static):
    def on_click(self) -> None:
        self.app.query_one(FollowScroll).follow()


class ConsoleApp(App):
    AUTO_FOCUS = None
    CSS = """
    Screen { background: #101820; color: #d8e2eb; layers: base above; }
    #heading { height: 1; background: #1b2a3a; color: #9fb4c4; padding: 0 1; text-wrap: nowrap;
               text-overflow: ellipsis; }
    #pinned { height: auto; background: #162330; color: #c3d0db; padding: 0 1; text-wrap: nowrap;
              text-overflow: ellipsis; }
    #rows { padding: 0 1; }
    #foot { height: 1; background: #1b2a3a; color: #7f93a4; padding: 0 1; text-wrap: nowrap;
            text-overflow: ellipsis; }
    #latest { layer: above; dock: bottom; display: none; width: 100%; height: 1; text-align: right;
              color: #7f93a4; padding: 0 2; }
    #latest:hover { color: #d8e2eb; }
    EvidenceRow { border: none; padding: 0; margin: 0; height: auto; }
    CollapsibleTitle { height: 1; padding: 0; text-wrap: nowrap; text-overflow: ellipsis; color: #c3d0db; }
    CollapsibleTitle:focus { background: #25384a; text-style: none; }
    .passed > CollapsibleTitle { color: #88d5a0; }
    .cracked > CollapsibleTitle { color: #5fe08a; text-style: bold; }
    .stop > CollapsibleTitle { color: #ffcd82; }
    .planned > CollapsibleTitle { color: #6f7d8a; }
    .raw > CollapsibleTitle { color: #8a9eb0; }
    .details { height: auto; padding: 0 1 0 6; color: #9db0bf; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit", show=False),
        Binding("j,down", "next_row", "Next", show=False),
        Binding("k,up", "previous_row", "Previous", show=False),
        Binding("space", "toggle_row", "Expand", show=False),
        Binding("home", "first_row", "First", show=False),
        Binding("end", "last_row", "Latest", show=False),
        Binding("f5", "restart", "Restart", show=False),
        Binding("f7", "pause", "Pause retry", show=False),
    ]

    def __init__(self, sources: Sources, supervisor=None, signals: SupervisorSignals | None = None):
        super().__init__(driver_class=ConsoleDriver)
        self.sources, self.supervisor, self.signals = sources, supervisor, signals
        self.widgets: dict[str, EvidenceRow] = {}
        self.watchers: dict[Path, int] = {}
        self.refresh_pending = False
        self.manage_task = None
        self.checked = time.time()
        self.heading_text = ""
        self.model: Dashboard | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="heading")
        yield Static("", id="pinned")
        yield FollowScroll(id="rows")
        yield Static("", id="foot")
        yield LatestControl("↓ latest", id="latest")

    async def on_mount(self) -> None:
        await self.refresh_sources()
        self.set_interval(1.0, self.tick)
        self.set_interval(10.0, self.source_changed)  # slow heartbeat: re-read even when no file event arrives
        if self.supervisor:
            self.supervisor.on_change = self.source_changed
            self.manage_task = asyncio.create_task(self.supervisor.manage())

    def show_heading(self) -> None:
        """One title line; `checked Ns ago` shows the console is alive between source changes."""
        line = Text(self.heading_text)
        line.append(f"  · {ago(time.time() - self.checked)}", style="dim")
        with suppress(NoMatches):  # a tick can land while the app is starting or closing
            self.query_one("#heading", Static).update(line)

    def tick(self) -> None:
        now = time.time()
        self.show_heading()
        for widget in self.widgets.values():
            widget.tick(now)

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
        self.checked = time.time()
        self.heading_text = model.heading + (("  " + model.summary) if model.summary else "")
        self.show_heading()
        pinned = self.query_one("#pinned", Static)
        pinned.update(Text("\n".join(model.pinned)))
        pinned.display = bool(model.pinned)
        foot = self.query_one("#foot", Static)
        foot.update(Text(model.footer))
        foot.display = bool(model.footer)
        container = self.query_one("#rows", FollowScroll)
        was_following = container.following
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
        if was_following:
            self.call_after_refresh(container.follow)
        if self.focused is None and self.widgets:  # keyboard rows from the start, without scrolling to them
            self._titles()[0].focus(scroll_visible=False)
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
        self.query_one(FollowScroll).follow()
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
    actors: tuple[str, ...] = (),
) -> None:
    status = (sources or {}).get("supervisor_status")
    signals = None
    if status:
        from abu_decomp.supervisor.supervisor import pidfile

        signals = SupervisorSignals(pidfile(Path(status)))
    ConsoleApp(
        Sources(
            path, title=title, config=sources, kinds=kinds, host_log=host_log, limit=min(max_lines, 200), actors=actors
        ),
        signals=signals,
    ).run()
