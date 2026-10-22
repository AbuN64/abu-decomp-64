"""Every `next` a finding prints is a command that runs as printed.

A `next` was free text in a dozen producers, and four of them were wrong in one TAS cycle (a build command that fails
configure while the pin lags, a ship line with a subject over the limit, a `--patch` form after a `--worktree`
refusal). This module is the one place that builds and checks them:

- `subject(text, limit)` cuts a commit subject to the configured limit on a word boundary.
- `ship_command(...)` writes the ship invocation in the same form that was refused (`--worktree` or `--patch`).
- `problems(command, limit)` reads a `next` back: a decomp command is parsed by the real argparse tree (so a flag or
  verb that does not exist is found), a command word must be one of `RUNNABLE`, no placeholder text may remain
  (`<what moved>`, `LANE=PATH`, `FILE`), and a commit message inside it must fit `limit`.
The unit test `tests/test_nextcmd.py` walks every producer and asserts `problems(...) == []` for each `next`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from abu_decomp import DecompError

DEFAULT_LIMIT = 72
# The programs a `next` may start with; anything else is prose.
RUNNABLE = frozenset(
    {"decomp", "recipe", "unbake", "taste", "behave", "git", "cmake", "make", "grep", "cat", "sh", "sed", "ls"}
)
_ALLOWED_CAPS = frozenset({"FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "CMAKE_BUILD_TYPE", "NON_MATCHING"})
_PLACEHOLDER = re.compile(
    r"<[^<>\s][^<>]*>|\bLANE=PATH\b|\b(?:FILE|NAME|SYM|MESSAGE|PATH|MOVIE|LANE)\b|\.\.\.|\b[A-Z]+(?:_[A-Z0-9]+)+\b"
)


def subject(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """`text`'s first line cut to `limit` characters at a word boundary (never mid-word, no trailing punctuation)."""
    line = " ".join(text.split("\n")[0].split())
    if len(line) <= limit:
        return line
    cut = line[:limit]
    if line[limit] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") or line[:limit]


def ship_command(
    profile: Path | str | None,
    tree: Path | str,
    lane: str,
    message: str | None = None,
    *,
    path: Path | str | None = None,
    worktree: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """The ship invocation for one input, in the form the refused one used (`--worktree NAME[:MSG]` or `--patch`)."""
    head = ["decomp"] + (["--profile", str(profile)] if profile else []) + ["ship", "--tree", str(tree)]
    msg = subject(message, limit) if message else ""
    if worktree:
        head += ["--worktree", f"{lane}:{msg}" if msg else lane]
    else:
        head += ["--patch", f"{lane}={path or lane + '.patch'}" + (f":{msg}" if msg else "")]
    return shlex.join(head)


def as_worktree(command: str, lane: str) -> str:
    """A ship command written with `--patch` becomes the `--worktree` form (a worktree ship refuses `--patch`)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if "ship" not in words or "--patch" not in words:
        return command
    index = words.index("--patch")
    _, _, message = words[index + 1].partition(":") if index + 1 < len(words) else ("", "", "")
    words[index : index + 2] = ["--worktree", f"{lane}:{message}" if message else lane]
    return shlex.join(words)


def read_site(where: str) -> str:
    """The command that shows a `file:line` site (the lines around it), or the whole file when no line is given."""
    file, _, line = where.partition(":")
    if line.isdigit():
        number = int(line)
        return f"sed -n {max(1, number - 3)},{number + 5}p {file}"
    return f"cat {file}"


def _messages(words: Sequence[str]) -> list[str]:
    found = []
    for index, word in enumerate(words):
        nxt = words[index + 1] if index + 1 < len(words) else ""
        if word in ("-m", "--message"):
            found.append(nxt)
        elif (word == "--patch" and ":" in nxt) or (word == "--worktree" and ":" in nxt):
            found.append(nxt.partition(":")[2])
    return [m.splitlines()[0] for m in found if m]


def parse_decomp(words: Sequence[str]) -> str | None:
    """None when the real decomp parser accepts `words` (the arguments after `decomp`), else its complaint."""
    from abu_decomp import cli

    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.parser().parse_args(list(words))
    except SystemExit as exc:
        if exc.code == 0:
            return None
        return (err.getvalue().strip().splitlines() or ["rejected"])[-1]
    except (argparse.ArgumentError, DecompError) as exc:  # the CLI's own parser raises DecompError on a bad argument
        return str(exc)
    return None


def problems(command: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Why `command` would not run as printed (empty when it would)."""
    if not command or not command.strip():
        return ["empty next"]
    try:
        words = shlex.split(command)
    except ValueError as exc:
        return [f"unquotable: {exc}"]
    found: list[str] = []
    if Path(words[0]).name not in RUNNABLE:
        return [f"not a command (starts with {words[0]!r})"]
    for hit in _PLACEHOLDER.findall(" ".join(w for w in words if not w.startswith("-D"))):
        if hit not in _ALLOWED_CAPS:
            found.append(f"placeholder {hit!r}")
    if any(w in ("&&", "|", ";") for w in words):
        found.append("a next is one command, not a chain")
    for message in _messages(words):
        if len(message) > limit:
            found.append(f"commit subject is {len(message)} characters, the limit is {limit}")
    if Path(words[0]).name == "decomp":
        complaint = parse_decomp(words[1:])
        if complaint:
            found.append(f"decomp parser: {complaint}")
    return found
