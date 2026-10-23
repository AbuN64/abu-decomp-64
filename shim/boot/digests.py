# SPDX-License-Identifier: GPL-3.0-or-later
"""Content hashes of files, remembered by (path, size, mtime) in a memo shared by every build tree.

A header read by thousands of units is hashed once for the whole machine, not once per unit or once per tree: a unit
step that checks its dependencies against a cached result costs one stat per file when the memo is warm. The memo is
only a shortcut: a file whose size or mtime changed is hashed again, a lost update (concurrent steps each save) is
hashed again, and an unwritable memo changes nothing but the speed."""
import hashlib
import json
import os

KEEP = 40000  # entries kept when a save finds the memo past 1.5x this (oldest dropped: stale worktree paths)


def digest_file(path):
    with open(path, "rb") as stream:  # file_digest reads in large blocks: about half the cost of read_bytes + sha256
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Digests:
    def __init__(self, memo):
        self.memo = memo
        try:
            with open(memo) as stream:
                self.known = json.load(stream)
        except (OSError, ValueError):
            self.known = {}
        self.added = {}

    def of(self, path):
        path = os.fspath(path)
        stat = os.stat(path)
        sig = [stat.st_size, stat.st_mtime_ns]
        entry = self.added.get(path) or self.known.get(path)
        if entry and entry[:2] == sig:
            return entry[2]
        value = digest_file(path)
        self.added[path] = [*sig, value]
        return value

    def save(self):
        if not self.added:
            return
        try:
            with open(self.memo) as stream:
                merged = json.load(stream)
        except (OSError, ValueError):
            merged = {}
        merged.update(self.added)
        if len(merged) > KEEP * 3 // 2:
            merged = dict(list(merged.items())[-KEEP:])
        temporary = f"{self.memo}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.memo) or ".", exist_ok=True)
            with open(temporary, "w") as stream:
                json.dump(merged, stream)
            os.replace(temporary, self.memo)
        except OSError:
            pass
        self.added = {}
