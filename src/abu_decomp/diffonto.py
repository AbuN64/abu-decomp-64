"""`decomp diff --onto REV PATH...`: a lane's change, rebuilt on a newer main.
A lane's tree carries earlier landed or queued work, so `git diff HEAD` is not what the train should apply. This takes
the change of PATHs (tree files against --from, default HEAD; --hunks keeps only hunks whose changed lines match a
regex; --function FN keeps only FN's own hunks: inside its definition or naming it, symbol rows included),
applies it with a 3-way merge in a scratch index seeded from REV, optionally renames a symbol in every
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


_CONFLICT = re.compile(
    r"^(?:U (\S+)|Applied patch to '([^']+)' with conflicts\.|error: patch failed: ([^:]+):\d+"
    r"|error: (\S+): (?:does not exist in index|already exists in index))",
    re.M,
)


def conflict_message(stderr: str, onto: str) -> str:
    """What a failed 3-way apply onto ONTO names: the files that conflict, then what to do (10-25 NAMES-2: the bare git
    error was cut off and named no file)."""
    files = sorted({next(g for g in m.groups() if g) for m in _CONFLICT.finditer(stderr)})
    if not files:
        return f"the change does not apply onto {onto}: {stderr.strip()[-400:]}"
    return (
        f"the change conflicts with {onto} in {len(files)} file(s): {', '.join(files[:12])}. Rebuild those files on a "
        f"fresh lease at {onto} (`git diff {onto} -- FILE` shows what landed there), or leave them out with PATHS"
    )


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


def function_span(text: str, fn: str) -> tuple[int, int] | None:
    """(first, last) 1-based lines of FN's definition in a C/C++ source: the line opening `FN(` outside a statement
    through the line where its braces balance. None when the file defines no such function."""
    lines = text.split("\n")
    head = re.compile(rf"^[A-Za-z_][\w\s\*]*\b{re.escape(fn)}\s*\(")
    for i, line in enumerate(lines):
        if not head.match(line):
            continue
        depth, seen = 0, False
        for j in range(i, len(lines)):
            code = re.sub(r"//.*|/\*.*?\*/|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'", "", lines[j])
            if not seen and j > i + 40:
                break
            if not seen and ";" in code and "{" not in code and depth == 0:
                break  # a prototype or a call, not the definition
            depth += code.count("{") - code.count("}")
            seen = seen or "{" in code
            if seen and depth <= 0:
                return i + 1, j + 1
    return None


def filter_function(diff: str, fn: str, read: Any) -> str:
    """Only the part of a -U0 `git diff` that belongs to function FN: the hunks of a source file that fall inside FN's
    definition (READ(path) gives the file's new text), every hunk with a changed line naming FN (its symbol, size and
    layout rows, a call site), and a file created or removed under FN's name. Everything else of the same files, which
    another queued diff may carry, is left out."""
    word = re.compile(rf"\b{re.escape(fn)}\b")
    out: list[str] = []
    head: list[str] = []
    hunk: list[str] = []
    state = {"path": "", "span": None, "whole": False}

    def flush() -> None:
        nonlocal hunk
        if hunk:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk[0])
            first = int(m.group(1)) if m else 0
            count = int(m.group(2)) if m and m.group(2) is not None else 1
            last = first + max(count, 1) - 1
            span = state["span"]
            inside = bool(span) and first <= span[1] and last >= span[0]
            named = any(line[:1] in "+-" and word.search(line) for line in hunk[1:])
            if state["whole"] or inside or named:
                out.extend(head)
                head.clear()
                out.extend(hunk)
        hunk = []

    for line in diff.rstrip("\n").split("\n"):
        if line.startswith("diff --git"):
            flush()
            head[:] = [line]
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            path = m.group(2) if m else ""
            state["path"] = path
            state["whole"] = bool(word.search(path))
            state["span"] = None
            if re.search(r"\.(c|cpp|cc|h)$", path, re.I) and not state["whole"]:
                try:
                    state["span"] = function_span(read(path), fn)
                except (OSError, ValueError):
                    state["span"] = None
        elif not hunk and line.startswith(
            ("index ", "--- ", "+++ ", "deleted file", "new file", "old mode", "new mode", "rename ", "similarity ")
        ):
            head.append(line)
        elif line.startswith("@@"):
            flush()
            hunk = [line]
        elif hunk:
            hunk.append(line)
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
    function: str | None = None,
    rename: str | None = None,
    msg: str | None = None,
    out: Path | None = None,
) -> dict[str, Any]:
    if not paths and not rename:
        raise DiffOntoError("diff.args", "name the PATHs to carry (or --rename-symbol OLD=NEW)")
    git(tree, "rev-parse", "--verify", "-q", f"{onto}^{{commit}}")
    part = (
        git(tree, "diff", "--binary", "-U0" if hunks or function else "-U3", base, "--", *paths).decode(
            errors="surrogateescape"
        )
        if paths
        else ""
    )
    if function:
        part = filter_function(part, function, lambda p: (tree / p).read_text(errors="replace"))
        if paths and not part:
            raise DiffOntoError("diff.function", f"no hunk of {' '.join(paths)} lies in or names {function}")
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
            argv = ["apply", "--cached", "-3", "--recount", *(["--unidiff-zero"] if hunks or function else [])]
            done = subprocess.run(
                ["git", "-C", str(tree), *argv],
                input=part.encode(errors="surrogateescape"),
                capture_output=True,
                env={**os.environ, **index},
            )
            if done.returncode:
                raise DiffOntoError("diff.conflict", conflict_message(done.stderr.decode(errors="replace"), onto))
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
