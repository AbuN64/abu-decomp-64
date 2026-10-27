"""Replay folders have a bounded life: the newest and the recent stay, the live stay, the rest go."""

import fcntl
import json
import os
import time
from pathlib import Path

from abu_decomp import tas


def make(plays: Path, name: str, age_hours: float, status: str = "done", size: int = 100) -> Path:
    run = plays / name
    run.mkdir(parents=True)
    (run / "big.bin").write_bytes(b"x" * size)
    (run / tas.RECORD).write_text(json.dumps({"status": status, "pid": 1, "lease": True}))
    when = time.time() - age_hours * 3600
    os.utime(run, (when, when))
    return run


def test_old_runs_beyond_the_newest_go_and_the_rest_stay(tmp_path):
    plays = tmp_path / "plays"
    old = [make(plays, f"20261020T00000{i}Z-m", 48) for i in range(5)]
    recent = make(plays, "20261027T000000Z-m", 1)
    newest_old = make(plays, "20261026T000000Z-m", 30)
    assert tas.prune_plays({"runs": tmp_path}, keep=2, hours=6) == 5
    assert not any(r.exists() for r in old)
    assert recent.exists() and newest_old.exists()  # the two newest by name stay whatever their age


def test_a_live_play_and_a_young_one_are_never_removed(tmp_path):
    plays = tmp_path / "plays"
    live = make(plays, "20261001T000000Z-live", 99, status="running")
    abandoned = make(plays, "20261001T000001Z-dead", 99, status="running")  # nobody holds its lease
    young = make(plays, "20261002T000000Z-young", 0.1)
    make(plays, "20261004T000000Z-newest", 99)
    with (live / "run.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        assert tas.prune_plays({"runs": tmp_path}, keep=1, hours=6) == 1
    assert live.exists() and young.exists() and not abandoned.exists()


def test_stray_gcov_files_older_than_the_window_go(tmp_path):
    plays = tmp_path / "plays"
    plays.mkdir()
    stale, fresh = plays / "a.CPP##1.gcov.json.gz", plays / "b.CPP##2.gcov.json.gz"
    for f, age in ((stale, 10), (fresh, 0.1)):
        f.write_bytes(b"z")
        os.utime(f, (time.time() - age * 3600,) * 2)
    tas.prune_plays({"runs": tmp_path}, keep=5, hours=6)
    assert not stale.exists() and fresh.exists()


def test_no_plays_folder_is_nothing_to_do(tmp_path):
    assert tas.prune_plays({"runs": tmp_path}) == 0
