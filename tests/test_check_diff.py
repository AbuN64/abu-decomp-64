"""`data check --diff`: applied to a clean tree for the check and taken off again; a dirty tree is left."""

import argparse
import subprocess
from pathlib import Path

from abu_decomp.data import check as check_mod
from abu_decomp.data import command


def git(tree, *a):
    env = {k: "t" for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")}
    env |= {"GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(tree), *a], capture_output=True, text=True, check=True, env=env).stdout


def make(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    git(tree, "init", "-q")
    (tree / "x.h").write_text("a\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "one")
    (tree / "x.h").write_text("b\n")
    diff = tmp_path / "d.diff"
    diff.write_text(git(tree, "diff"))
    git(tree, "checkout", "-q", "--", "x.h")
    return tree, diff


def args(tree, diff):
    return argparse.Namespace(tree=tree, diff=diff, write_baseline=False)


def test_a_diff_is_applied_to_a_clean_tree_for_the_check_only(tmp_path):
    tree, diff = make(tmp_path)
    assert command._apply_diff(args(tree, diff)) is True and (tree / "x.h").read_text() == "b\n"
    subprocess.run(["git", "-C", str(tree), "apply", "-R", str(diff)], check=True)
    assert (tree / "x.h").read_text() == "a\n"


def test_a_tree_that_already_holds_changes_is_left_as_it_is(tmp_path):
    tree, diff = make(tmp_path)
    (tree / "x.h").write_text("b\n")  # the lane's own tree: the diff was made from it
    assert command._apply_diff(args(tree, diff)) is False and (tree / "x.h").read_text() == "b\n"
    assert command._apply_diff(argparse.Namespace(tree=tree, diff=None, write_baseline=False)) is False


def test_the_check_always_brings_the_extraction_current(monkeypatch, tmp_path):
    seen = []
    from abu_decomp import tools

    monkeypatch.setattr(tools, "invoke", lambda *a, **k: seen.append(a) or type("R", (), {"ok": True})())
    assert check_mod.reextract(None, Path(tmp_path), [], always=True) is None and len(seen) == 1
    assert check_mod.reextract(None, Path(tmp_path), ["src/a.c"]) is None and len(seen) == 1
