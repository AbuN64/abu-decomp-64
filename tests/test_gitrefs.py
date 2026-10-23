import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from abu_decomp.gitrefs import RefSource


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
