"""A lane or stamp tree is copied from the shared tree under its lock held shared, so an extraction (which `unbake setup`
runs under the lock held exclusively) is never copied half-written (10-25: a stamp seed lacked undefined_syms_auto.txt)."""

import subprocess
import sys
import threading
import time

import pytest

from abu_decomp import worktree

WRITER = """
import sys, time
from pathlib import Path
from unbake import treelock
shared = Path(sys.argv[1])
with treelock.hold(shared, exclusive=True, who="unbake setup (extracting)", say=lambda m: None):
    (shared / "writing").write_text("1")
    while not (shared / "release").exists():
        time.sleep(0.05)
    (shared / "build/de/undefined_syms_auto.txt").write_text("D_1 = 0x1;\\n")
"""


def test_a_seed_waits_for_an_extraction_that_holds_the_shared_tree(tmp_path):
    shared, target = tmp_path / "shared", tmp_path / "lane"
    (shared / "build/de").mkdir(parents=True)
    (shared / "asm/de").mkdir(parents=True)
    target.mkdir()
    writer = subprocess.Popen([sys.executable, "-c", WRITER, str(shared)])
    try:
        for _ in range(200):
            if (shared / "writing").exists():
                break
            time.sleep(0.05)
        done = []
        seeder = threading.Thread(target=lambda: done.append(worktree.seed_locked(shared, target, "STAMP-S0")))
        seeder.start()
        time.sleep(0.5)
        assert not done  # the extraction holds the tree: no copy yet
        (shared / "release").write_text("1")
        writer.wait(10)
        seeder.join(10)
        assert done and (target / "build/de/undefined_syms_auto.txt").read_text() == "D_1 = 0x1;\n"
    finally:
        writer.kill()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
