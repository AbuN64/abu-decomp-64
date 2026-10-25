"""`data stamp --build`: a diff is built alone on a fresh warm tree and stamped only on a clean all-version pass."""

import subprocess

from abu_decomp import worktree
from abu_decomp.data import stamp as stamp_mod
from abu_decomp.data import stampbuild
from abu_decomp.data.config import DataConfig

CFG = DataConfig(reference="a", versions=("a", "b"), source_suffixes=(".c",))
DIFF = (
    "diff --git a/include/x.h b/include/x.h\n--- a/include/x.h\n+++ b/include/x.h\n"
    "@@ -1 +1 @@\n-extern int D_1;\n+extern long D_1;\n"
)


def setup(tmp_path, monkeypatch, broken):
    tree = tmp_path / "wt" / "STAMP-x"
    tree.mkdir(parents=True)
    pruned = []
    monkeypatch.setattr(worktree, "new", lambda p, s, lane, at: ({"worktree": str(tree), "rev": "a" * 40}, 0, []))
    monkeypatch.setattr(worktree, "prune", lambda s, lanes, *a: pruned.append(lanes) or ({}, 0, []))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    from abu_decomp.data import check as check_mod

    monkeypatch.setattr(
        check_mod,
        "check",
        lambda b, v: {"version": v, "built": True, "broken": [["u.c", "f", "edit=1"]] if broken else []},
    )
    diff = tmp_path / "d.diff"
    diff.write_text(DIFF)
    return diff, pruned


def test_clean_pass_writes_the_stamp_and_removes_the_tree(tmp_path, monkeypatch):
    diff, pruned = setup(tmp_path, monkeypatch, False)
    _result, code, findings = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    assert code == 0 and findings == []
    assert stamp_mod.stamped(tmp_path / "stamps", DIFF) and pruned[-1][0].startswith("STAMP-")
    assert stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")[0] == {"stamp": "present"}


def test_a_broken_function_refuses_the_stamp(tmp_path, monkeypatch):
    diff, pruned = setup(tmp_path, monkeypatch, True)
    _result, code, findings = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    assert code == 1 and "u.c f edit=1" in findings[0]["message"]
    assert not stamp_mod.stamped(tmp_path / "stamps", DIFF) and pruned


def test_the_tree_is_made_at_the_shared_trees_commit_and_falls_back_to_the_tip(tmp_path, monkeypatch):
    diff, _pruned = setup(tmp_path, monkeypatch, False)
    ats = []
    tree = tmp_path / "wt" / "STAMP-x"
    monkeypatch.setattr(
        worktree, "new", lambda p, s, lane, at: ats.append(at) or ({"worktree": str(tree), "rev": "a" * 40}, 0, [])
    )
    applies = iter([1, 0])  # the diff does not apply on the shared commit, then applies on the tip

    def run(cmd, **k):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "c0ffee\n", "")
        return subprocess.CompletedProcess(cmd, next(applies) if "apply" in cmd else 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    _r, code, _f = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    assert code == 0 and ats == ["c0ffee", None]


RENAME = (
    "diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n@@ -1,2 +1,2 @@\n"
    "-extern void func_80000000_de(int); /* osX */\n+extern void osX(int);\n"
    "diff --git a/versions/us/symbol_addrs.txt b/versions/us/symbol_addrs.txt\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
    "-func_80000000_de = 0x80000000; // type:func\n+osX = 0x80000000; // type:func\n"
    "diff --git a/versions/us/names.tsv b/versions/us/names.tsv\n--- a/x\n+++ b/x\n"
    "@@ -1 +1,2 @@\n x\n+0x80000000\tosX\tcode\n"
)


def test_a_pure_rename_diff_touches_no_data():
    assert stamp_mod.pure_rename(RENAME, (".c",))
    assert not stamp_mod.touches_data(CFG, RENAME)
    retyped = RENAME.replace("+extern void osX(int);", "+extern long osX(int);")
    assert not stamp_mod.pure_rename(retyped, (".c",))
    assert stamp_mod.touches_data(CFG, retyped)
