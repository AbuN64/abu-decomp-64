"""A game repo builds with make alone: none of its files names one of our tools.

The names are those of the tools decomp pins (tools.TOOLS: each distribution and module) and of decomp itself, plus
the repositories the pins point at, matched in any case and with any `-`, `_` or space between their letters (so
`cake-unbake`, `__UNBAKE_X` and `recipe maker` all count). A plain command word (`recipe`, `taste`, `decomp`) is
not a name: game text and make recipes use those words. `mentions(tree)` reads every tracked and untracked
unignored file of the tree; `mentions(tree, against=REV)` reads only the lines the work tree adds over REV, which is
what a landing or a hand commit is judged on. The whole-tree form is the proof that a repo is clean."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Collection
from contextlib import suppress
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tools

_OWNER = re.compile(r"^abu[-_]")  # the owner word some repository names start with; the rest is the tool's name


def _stems() -> set[str]:
    names = {tools.DISTRIBUTION, __name__.split(".")[0]}
    for tool in tools.TOOLS.values():
        names |= {tool.distribution, tool.module.split(".")[0]}
    with suppress(DecompError):  # not installed with its pins (a test, a source checkout): the names still hold
        names |= {pin.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") for pin in tools.pins().values()}
    stems = set()
    for name in names:
        stem = re.sub(r"[-_]64$", "", name.lower())
        bare = _OWNER.sub("", stem)
        stems |= {stem, *([bare] if re.search(r"[-_]", bare) else [])}  # "recipe-maker" yes, a lone "decomp" no
    return stems


def pattern() -> str:
    """One extended regular expression (case-insensitive use) matching any spelling of a tool name."""
    words = sorted({re.sub(r"[^a-z0-9]", "", s) for s in _stems()})
    return "|".join("[-_ ]?".join(word) for word in words)


def _git(tree: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, errors="replace")
    if done.returncode not in (0, 1):
        raise DecompError("standalone.git", f"git {args[0]}: {done.stderr.strip()[-300:]}")
    return done.returncode, done.stdout


def mentions(tree: Path, against: str | None = None, untracked: Collection[str] | None = None) -> list[dict[str, Any]]:
    """Each line naming a tool: {path, line, text}. With `against`, only lines the work tree adds over it, and of
    the untracked files only those in `untracked` when given (the files a landing commits); every one otherwise."""
    expr = pattern()
    found: list[dict[str, Any]] = []
    if against is None:
        _, out = _git(tree, "grep", "-I", "-n", "-i", "-E", "--untracked", "-e", expr)
        for row in out.splitlines():
            path, line, text = row.split(":", 2)
            found.append({"path": path, "line": int(line), "text": text[:200]})
        return found
    matcher = re.compile(expr, re.I)
    _, diff = _git(tree, "diff", against, "-U0", "--no-color", "--no-renames", "--no-ext-diff")
    path, line = None, 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
        elif row.startswith("@@"):
            line = int(re.match(r"@@ -\S+ \+(\d+)", row)[1])
        elif row.startswith("+") and path is not None:
            if matcher.search(row[1:]):
                found.append({"path": path, "line": line, "text": row[1:][:200]})
            line += 1
    _, others = _git(tree, "ls-files", "--others", "--exclude-standard")
    for name in others.splitlines():
        if untracked is not None and name not in untracked:
            continue
        try:
            text = (tree / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a binary or unreadable file names nothing
        found += [
            {"path": name, "line": n, "text": t[:200]} for n, t in enumerate(text.splitlines(), 1) if matcher.search(t)
        ]
    return found


def finding(found: list[dict[str, Any]], key: str) -> dict[str, Any]:
    paths = sorted({f["path"] for f in found})
    return {
        "key": key,
        "message": f"{len(found)} line(s) in {len(paths)} file(s) name one of our tools; a game repo builds with make "
        "alone and names none: " + ", ".join(paths[:8]),
        "blocking": True,
        "lines": found[:20],
    }
