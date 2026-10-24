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
