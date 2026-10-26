"""`decomp worktree new` writes the pool's marker (`build/.pool-extracted-at`) like `pool-mint.sh --lease`, so a data
check on the lane's tree narrows instead of silently proving only the changed units (10-26 CLEAN-P1-DECLS14)."""

import subprocess

from abu_decomp import worktree


def _git(tree, *args):
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _git(shared, "init", "-q")
    (shared / "a.c").write_text("int a;\n")
    _git(shared, "-c", "user.name=t", "-c", "user.email=t@t", "add", "a.c")
    _git(shared, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    target = tmp_path / "lane"
    _git(shared, "worktree", "add", "--detach", "-q", str(target), "HEAD")
    return shared, target, _git(shared, "rev-parse", "HEAD")


def test_new_marks_the_tree_with_its_commit_and_keeps_a_current_built_marker(tmp_path):
    shared, target, rev = _repo(tmp_path)
    (target / "build").mkdir()
    (target / "build/.pool-built-at").write_text(rev + "\n")
    path = worktree.mark(shared, target, rev)
    assert path == str(target / "build/.pool-extracted-at")
    assert (target / "build/.pool-extracted-at").read_text().strip() == rev
    assert (target / "build/.pool-extracted-at").stat().st_mtime > 0
    assert (target / "build/.pool-built-at").read_text().strip() == rev


def test_a_seed_from_other_content_gets_a_marker_no_unit_counts_as_untouched_under_and_loses_the_built_marker(tmp_path):
    shared, target, rev = _repo(tmp_path)
    (shared / "a.c").write_text("int b;\n")  # the shared tree holds other content than REV: its objects are not REV's
    (target / "build").mkdir()
    (target / "build/.pool-built-at").write_text(rev + "\n")
    worktree.mark(shared, target, rev)
    assert not (target / "build/.pool-built-at").exists()
    assert (target / "build/.pool-extracted-at").read_text().strip() == rev
    assert (target / "build/.pool-extracted-at").stat().st_mtime == 0
