import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from abu_decomp.console.app import selected_events
from abu_decomp.console.feed import append_event, canonical_kind
from abu_decomp.console.gitrefs import RefSource
from abu_decomp.console.host_output import HostOutput
from abu_decomp.console.statusline import follow
from abu_decomp.console.terminal import resize_to_tty
from abu_decomp.console.wrap import WrappedLog


class StatusLineTests(unittest.TestCase):
    def test_status_line_prints_latest_event_and_a_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            out = io.StringIO()
            follow(path, out, iterations=1)
            assert out.getvalue().strip() == "no events yet"
            row = {"schema": 1, "time": "2026-01-01T00:00:00+00:00", "actor": "git", "status": "info", "message": "m"}
            path.write_text(json.dumps(row) + "\n")
            out = io.StringIO()
            follow(path, out, iterations=1)
            assert "[git/INFO] m" in out.getvalue()


class StatusButtonTests(unittest.TestCase):
    def test_bar_speaks_i3bar_blocks_and_a_click_runs_the_button_command(self):
        buttons = [{"label": "RESTART GAME", "command": ["stream", "restart", "port-supervisor"]}]
        ran = []
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            clicks = ["[\n", '{"name":"event","button":1},\n', '{"name":"button-0","button":1}\n']
            follow(Path(tmp) / "e.jsonl", out, iterations=1, buttons=buttons, run=ran.append, clicks=clicks)
            lines = out.getvalue().splitlines()
            assert json.loads(lines[0]) == {"version": 1, "click_events": True}
            assert lines[1] == "["
            row = json.loads(lines[2].rstrip(","))
            assert [block["name"] for block in row] == ["button-0", "event"]
            assert row[0]["full_text"] == " RESTART GAME "
            deadline = 50
            while not ran and deadline:
                threading.Event().wait(0.02)
                deadline -= 1
        assert ran == [["stream", "restart", "port-supervisor"]]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit(repo, subject):
    (repo / "f").write_text(subject)
    git(repo, "add", "f")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", subject)


class RefSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        for subject in ("one", "two", "three", "four"):
            commit(self.repo, subject)
        self.events = []
        self.profile = SimpleNamespace(resolve_path=lambda value: self.root / value)
        self.config = {"repos": [{"name": "r", "path": "repo"}], "state_path": "seen.json", "seed_commits": 2}

    def tearDown(self):
        self.tmp.cleanup()

    def source(self):
        return RefSource(
            self.profile, self.config, lambda _p, actor, status, message, **kw: self.events.append(message)
        )

    def test_first_scan_seeds_latest_commits_then_only_new_ones(self):
        source = self.source()
        assert source.scan() == 2
        assert [m.split(" ", 2)[2] for m in self.events] == ["three", "four"]
        assert source.scan() == 0
        commit(self.repo, "five")
        commit(self.repo, "six")
        assert source.scan() == 2
        assert [m.split(" ", 2)[2] for m in self.events[2:]] == ["five", "six"]
        assert self.events[-1].startswith("r ")

    def test_a_checkout_that_cannot_fast_forward_is_a_run_fail_once_per_reason_and_clears_when_fixed(self):
        clone = self.root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True, capture_output=True)
        commit(clone, "local only")  # diverges once origin moves on
        (clone / "f").write_text("dirty")
        commit(self.repo, "upstream")
        self.config = {"repos": [{"name": "r", "path": "clone"}], "state_path": "seen.json"}
        failures = []
        source = RefSource(
            self.profile, self.config, lambda _p, actor, status, message, **kw: failures.append((status, message, kw))
        )
        source.pull()
        source.pull()
        assert len(failures) == 1
        status, message, kw = failures[0]
        assert (status, kw["kind"]) == ("failed", "RUN-FAIL")
        assert message.startswith("r cannot fast-forward: ahead 1, behind 1, 1 uncommitted file(s)")
        git(clone, "checkout", "-q", "--", "f")
        git(clone, "reset", "-q", "--hard", "origin/main")
        source.pull()
        assert len(failures) == 1 and not source.stuck

    def test_seen_state_survives_restart(self):
        self.source().scan()
        self.events.clear()
        assert self.source().scan() == 0

    def test_new_commit_wakes_serve_via_inotify(self):
        source = self.source()
        source.scan()
        self.events.clear()
        worker = threading.Thread(target=source.serve, kwargs={"iterations": 1}, daemon=True)
        worker.start()
        commit(self.repo, "late")
        worker.join(5)
        assert not worker.is_alive()
        assert any(m.endswith("late") for m in self.events)


class FeedTests(unittest.TestCase):
    def test_host_output_suppresses_spam_and_rate_limits_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.log"
            path.write_text(
                "n64host: cpu_cadence vi=600 count_ticks=42 entries=22 native_ns=9\n"
                "n64host: screen updates=600 elapsed_ms=10000 graphics_tasks=200\n"
                "n64host: keydown scancode=2\nn64host: WARNING bad task\n"
            )
            reader = HostOutput(path)
            self.assertEqual(
                reader.read_new(now=0),
                ["HOST n64host: WARNING bad task", "HOST heartbeat frames=600 graphics_tasks=200 VI=600"],
            )
            with path.open("a") as stream:
                stream.write("n64host: cpu_cadence vi=660\nn64host: fatal shader error\n")
            self.assertEqual(reader.read_new(now=59), ["HOST n64host: fatal shader error"])
            with path.open("a") as stream:
                stream.write("n64host: cpu_cadence vi=720\n")
            self.assertEqual(reader.read_new(now=60), ["HOST heartbeat frames=600 graphics_tasks=200 VI=720"])
            self.assertEqual(reader.read_new(now=120), [])
            path.write_text("n64host: failed open\n")
            self.assertEqual(reader.read_new(now=121), ["HOST n64host: failed open"])

    def test_selected_events_filter_by_schema_2_kind_and_read_schema_1_names(self):
        old = {"schema": 1, "time": "2026-01-01T00:00:00+00:00", "actor": "git", "status": "info", "evidence": None}
        records = [
            {**old, "message": "noise"},
            {**old, "message": "repo abc subject", "kind": "COMMIT"},
            {**old, "message": "rung", "kind": "RUNG"},
        ]
        self.assertEqual(canonical_kind(records[1]), "LAND")
        self.assertEqual(selected_events(records, {"LAND"}), [records[1]])
        self.assertEqual(selected_events(records, None), records)

    def test_append_through_the_one_writer_reaches_the_seat_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = SimpleNamespace(section=lambda _: {"path": "feed.jsonl"}, resolve_path=lambda v: root / v)
            written = append_event(profile, "git", "info", "r abc subject", kind="LAND")
            line = json.loads((root / "feed.jsonl").read_text())
            self.assertEqual(line, written["event"])
            self.assertEqual((line["schema"], line["kind"]), (2, "LAND"))


class WrapAndTerminalTests(unittest.TestCase):
    def test_wrapped_scroll_stays_anchored_then_follows_tail(self):
        view = WrappedLog()
        for number in range(12):
            view.append(f"actual event {number} /long/path/to/evidence")
        view.layout(20, 4)
        view.page(-1)
        anchor = view.layout(20, 4)[0]
        view.append("new outcome")
        assert view.layout(20, 4)[0] == anchor
        view.end()
        assert "new outcome" in " ".join(view.layout(20, 4))

    def test_terminal_resize_passes_lines_before_columns(self):
        with (
            patch("abu_decomp.console.terminal.os.get_terminal_size", return_value=os.terminal_size((106, 28))),
            patch("abu_decomp.console.terminal.curses.resizeterm") as resize,
        ):
            resize_to_tty(0)
        resize.assert_called_once_with(28, 106)
