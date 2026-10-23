"""A lane's work trees across its lifecycle (`lanes.worktrees` in the seat profile): `decomp lane continue` hands the closed
predecessor's trees (7.5 GB each, warm) to the new lane instead of leaving them behind, and `decomp lane end` removes a
lane's trees. Both save a diff of everything the tree holds beyond the remote main first; a tree that is in use, or
whose diff cannot be saved, stays. (Worktree creation and ship pruning are worktree.py.)"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError


class WorktreeError(DecompError):
    pass


def _git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=False)
    if done.returncode:
        raise WorktreeError("worktree.git", f"git {' '.join(args[:3])}: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def _referenced(shared: Path, path: Path) -> list[str]:
    """A queued ship's published input, or a running ship, names a file inside the tree."""
    why = []
    inputs = shared / ".git" / "decomp-ship.inputs"
    for entry in sorted(inputs.glob("*.json")) if inputs.is_dir() else []:
        if str(path) in entry.read_text(errors="replace"):
            why.append(f"queued ship input {entry.name}")
    return why


def _shared_of(tree: Path) -> Path:
    return Path(_git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).parent


def lane_trees(base: Path, lane: str) -> list[Path]:
    """A lane's work trees under `base`: `LANE` and `LANE-*` (a lane may own a unit-specific tree beside its main one)."""
    found = [base / lane] if (base / lane / ".git").exists() else []
    return found + [p for p in sorted(base.glob(f"{lane}-*")) if (p / ".git").exists()]


def _in_use(shared: Path, tree: Path) -> list[str]:
    """Why a tree may not move or go: a queued ship names it, or a live process runs inside it."""
    why = _referenced(shared, tree)
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            cwd = (proc / "cwd").resolve()
        except OSError:
            continue
        if cwd == tree or tree in cwd.parents:
            why.append(f"process {proc.name} runs in it")
    return why


def save_diff(tree: Path, dest: Path) -> Path:
    """Write everything the tree holds beyond the shared remote's main (local commits, edits and new files) to
    DEST/saved-<tree name>.diff, using a scratch index so the tree is not touched. Raises when it cannot."""
    base = "HEAD"
    for ref in ("github/main", "origin/main"):
        done = subprocess.run(
            ["git", "-C", str(tree), "merge-base", "HEAD", ref], capture_output=True, text=True, check=False
        )
        if done.returncode == 0:
            base = done.stdout.strip()
            break
    scratch = dest / f".saved-{tree.name}.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
    try:
        for args in (["read-tree", base], ["add", "-A", "--", "."]):
            done = subprocess.run(["git", "-C", str(tree), *args], env=env, capture_output=True, text=True, check=False)
            if done.returncode:
                raise WorktreeError("worktree.save", f"git {args[0]} in {tree}: {done.stderr.strip()[-200:]}")
        done = subprocess.run(
            ["git", "-C", str(tree), "diff", "--cached", "--binary", base], env=env, capture_output=True, check=False
        )
        if done.returncode:
            raise WorktreeError("worktree.save", f"git diff in {tree}: {done.stderr.decode(errors='replace')[-200:]}")
    finally:
        scratch.unlink(missing_ok=True)
    head = _git(tree, "rev-parse", "HEAD").strip()
    out = dest / f"saved-{tree.name}.diff"
    out.write_bytes(f"# TREE {tree} HEAD {head} BASE {base}\n".encode() + done.stdout)
    return out


def adopt(base: Path, old: str, new: str, dest: Path) -> list[dict[str, Any]]:
    """Hand the closed lane OLD's work trees to its continuation NEW: save a diff first, then `git worktree move` each
    tree to NEW's name (build outputs and uncommitted work stay; no second 7.5 GB tree). A tree in use stays put."""
    moved = []
    for tree in lane_trees(base, old):
        target = base / (new + tree.name[len(old) :])
        entry: dict[str, Any] = {"from": str(tree), "to": str(target)}
        shared = _shared_of(tree)
        if target.exists():
            entry["kept"] = "target exists"
        elif why := _in_use(shared, tree):
            entry["kept"] = "; ".join(why)
        else:
            entry["saved"] = str(save_diff(tree, dest))
            _git(shared, "worktree", "move", str(tree), str(target))
        moved.append(entry)
    return moved


def release(base: Path, lane: str, dest: Path) -> list[dict[str, Any]]:
    """A closed lane's trees: save the diff, then remove the tree. A tree in use, or whose diff cannot be saved, stays."""
    gone = []
    for tree in lane_trees(base, lane):
        entry: dict[str, Any] = {"tree": str(tree)}
        shared = _shared_of(tree)
        try:
            if why := _in_use(shared, tree):
                entry["kept"] = "; ".join(why)
            else:
                entry["saved"] = str(save_diff(tree, dest))
                _git(shared, "worktree", "remove", "--force", str(tree))
                _git(shared, "worktree", "prune")
                entry["removed"] = True
        except WorktreeError as exc:
            entry["kept"] = exc.detail
        gone.append(entry)
    return gone
