"""`decomp diff --onto REV PATH...`: a lane's change, rebuilt on a newer main.
A lane's tree carries earlier landed or queued work, so `git diff HEAD` is not what the train should apply. This takes
the change of PATHs (tree files against --from, default HEAD; --hunks keeps only hunks whose changed lines match a
regex), applies it with a 3-way merge in a scratch index seeded from REV, optionally renames a symbol in every
version's symbol file, and writes the diff REV -> result (with a `# MSG:` first line when --msg is given) to --out.
The tree, its index and its HEAD are never touched: the scratch index is a private file and the probe commit is a
dangling object.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

SYMBOL_FILES = "versions/*/symbol_addrs.txt"


class DiffOntoError(DecompError):
    pass


def git(tree: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None) -> bytes:
    done = subprocess.run(
        ["git", "-C", str(tree), *args], input=data, capture_output=True, env={**os.environ, **(env or {})}
    )
    if done.returncode:
        raise DiffOntoError(
            "diff.git", f"git {' '.join(args[:3])}: {done.stderr.decode(errors='replace').strip()[:400]}"
        )
    return done.stdout


def filter_hunks(diff: str, pattern: str) -> str:
    """Only the hunks of a `git diff` with a changed (+/-) line matching `pattern`; a file with none is dropped."""
    rx = re.compile(pattern)
    out: list[str] = []
    head: list[str] = []
    hunk: list[str] = []
    keep = False

    def flush() -> None:
        nonlocal hunk, keep
        if hunk and keep:
            out.extend(head)
            head.clear()
            out.extend(hunk)
        hunk, keep = [], False

    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            flush()
            head[:] = [line]
        elif not hunk and line.startswith(
            ("index ", "--- ", "+++ ", "deleted file", "new file", "old mode", "new mode")
        ):
            head.append(line)
        elif line.startswith("@@"):
            flush()
            hunk = [line]
        elif hunk:
            hunk.append(line)
            if line[:1] in "+-" and rx.search(line):
                keep = True
    flush()
    return "\n".join(out) + "\n" if out else ""


def rename_symbol(tree: Path, rev: str, index: dict[str, str], old: str, new: str) -> list[str]:
    """OLD -> NEW at the start of a line in every version's symbol file at REV, written into the scratch index."""
    changed = []
    names = git(tree, "ls-tree", "-r", "--name-only", rev, env=index).decode().split("\n")
    for path in (n for n in names if re.fullmatch(r"versions/[^/]+/symbol_addrs\.txt", n)):
        text = git(tree, "show", f"{rev}:{path}").decode()
        updated = re.sub(rf"^{re.escape(old)}(\s*=)", rf"{new}\1", text, flags=re.M)
        if updated != text:
            blob = git(tree, "hash-object", "-w", "--stdin", data=updated.encode()).decode().strip()
            git(tree, "update-index", "--cacheinfo", f"100644,{blob},{path}", env=index)
            changed.append(path)
    return changed


def run(
    tree: Path,
    onto: str,
    paths: list[str],
    *,
    base: str = "HEAD",
    hunks: str | None = None,
    rename: str | None = None,
    msg: str | None = None,
    out: Path | None = None,
) -> dict[str, Any]:
    if not paths and not rename:
        raise DiffOntoError("diff.args", "name the PATHs to carry (or --rename-symbol OLD=NEW)")
    git(tree, "rev-parse", "--verify", "-q", f"{onto}^{{commit}}")
    part = (
        git(tree, "diff", "--binary", "-U0" if hunks else "-U3", base, "--", *paths).decode(errors="surrogateescape")
        if paths
        else ""
    )
    if hunks:
        part = filter_hunks(part, hunks)
        if paths and not part:
            raise DiffOntoError("diff.hunks", f"no hunk of {' '.join(paths)} has a changed line matching {hunks!r}")
    if paths and not part.strip():
        raise DiffOntoError("diff.empty", f"{' '.join(paths)} has no change against {base}")
    with tempfile.TemporaryDirectory(prefix="diff-onto-") as scratch:
        index = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        git(tree, "read-tree", onto, env=index)
        if part.strip():
            git(
                tree,
                "apply",
                "--cached",
                "-3",
                "--recount",
                *(["--unidiff-zero"] if hunks else []),
                env=index,
                data=part.encode(errors="surrogateescape"),
            )
        renamed = []
        if rename:
            old, eq, new = rename.partition("=")
            if not eq or not old or not new:
                raise DiffOntoError("diff.args", "--rename-symbol takes OLD=NEW")
            renamed = rename_symbol(tree, onto, index, old, new)
            if not renamed:
                raise DiffOntoError("diff.rename", f"{old} starts no line of {SYMBOL_FILES} at {onto}")
        result = git(tree, "write-tree", env=index).decode().strip()
    diff = git(tree, "diff", "--binary", onto, result)
    if not diff:
        raise DiffOntoError("diff.empty", f"the change is already in {onto}")
    stat = git(tree, "diff", "--stat", onto, result).decode().strip().split("\n")[-1]
    probe = git(tree, "commit-tree", result, "-p", onto, "-m", "probe").decode().strip()
    answer: dict[str, Any] = {"onto": onto, "tree": result, "probe": probe, "stat": stat, "renamed": renamed}
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes((f"# MSG: {msg}\n".encode() if msg else b"") + diff)
        answer["out"] = str(out)
    return answer
