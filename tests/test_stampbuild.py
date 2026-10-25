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


def test_a_shared_tree_under_wt_keeps_lane_trees_beside_it_not_in_wt_wt(tmp_path):
    assert worktree.base(tmp_path / "code" / "wt" / "POOL-SRC") == tmp_path / "code" / "wt"
    assert worktree.base(tmp_path / "code" / "main") == tmp_path / "code" / "wt"


def test_slots_are_exclusive_and_a_full_pool_times_out(tmp_path):
    import pytest

    from abu_decomp import DecompError

    with stampbuild.slot(tmp_path, 2, 0) as one, stampbuild.slot(tmp_path, 2, 0) as two:
        assert {one, two} == {"STAMP-S0", "STAMP-S1"}
        with pytest.raises(DecompError), stampbuild.slot(tmp_path, 2, 0):
            pass
    with stampbuild.slot(tmp_path, 2, 0) as again:
        assert again == "STAMP-S0"


def test_a_slot_whose_tree_still_has_a_working_process_is_skipped(tmp_path):
    # the orphan of a stamp killed by a cap: its flock is gone but its make still runs inside STAMP-S0
    (tmp_path / "STAMP-S0" / "build").mkdir(parents=True)
    child = subprocess.Popen(["sleep", "30"], cwd=tmp_path / "STAMP-S0" / "build")
    try:
        assert stampbuild.busy_in(tmp_path / "STAMP-S0") == [child.pid]
        assert stampbuild.busy_in(tmp_path / "STAMP-S1") == []
        with stampbuild.slot(tmp_path, 2, 0) as lane:
            assert lane == "STAMP-S1"
    finally:
        child.kill()
        child.wait()
    with stampbuild.slot(tmp_path, 2, 0) as lane:
        assert lane == "STAMP-S0"


def git_tree(path):
    import os

    env = dict(
        os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"
    )
    path.mkdir(parents=True)
    for cmd in (["init", "-q"], ["add", "-A"]):
        (path / "x.h").write_text("a\n")
        subprocess.run(["git", "-C", str(path), *cmd], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "one"], check=True, env=env)
    (path / "x.h").write_text("b\n")
    subprocess.run(["git", "-C", str(path), "commit", "-qam", "two"], check=True, env=env)
    return [
        subprocess.run(["git", "-C", str(path), "rev-parse", r], capture_output=True, text=True).stdout.strip()
        for r in ("HEAD~1", "HEAD")
    ]


def test_a_clean_slot_tree_is_reused_only_at_its_own_commit(tmp_path):
    first, second = git_tree(tmp_path / "t")
    tree = tmp_path / "t"
    assert stampbuild.reuse(None, tree, second) is None  # no marker: never released, so not trusted
    (tree / stampbuild.STATE).write_text("busy\n")
    assert stampbuild.reuse(None, tree, second) is None
    (tree / stampbuild.STATE).write_text(first + "\n")  # an older commit: a fresh warm tree, no setup here
    assert stampbuild.reuse(None, tree, second) is None
    (tree / stampbuild.STATE).write_text(second + "\n")
    (tree / "x.h").write_text("diff applied\n")  # what a stamp build left behind
    (tree / "new.h").write_text("untracked\n")
    made = stampbuild.reuse(None, tree, second)
    assert made["reused"] and made["rev"] == second
    assert (tree / "x.h").read_text() == "b\n" and not (tree / "new.h").exists()


def test_release_reverts_the_tree_and_records_its_commit(tmp_path):
    _first, second = git_tree(tmp_path / "t")
    tree = tmp_path / "t"
    (tree / "x.h").write_text("diff\n")
    stampbuild.release(tmp_path, tree)
    assert (tree / "x.h").read_text() == "b\n" and (tree / stampbuild.STATE).read_text().strip() == second


def test_a_unit_whose_object_equals_the_clean_trees_is_not_scored(tmp_path, monkeypatch):
    from abu_decomp.data import check as check_mod
    from abu_decomp.data import trial

    cfg = DataConfig(
        reference="a", versions=("a",), source_suffixes=(".c",), object="build/{v}/{unit}.o", map_file="m.json"
    )
    tree, clean = tmp_path / "tree", tmp_path / "clean"
    for root, body in ((tree, b"same"), (clean, b"same")):
        (root / "build/a/src").mkdir(parents=True)
        (root / "build/a/src/x.c.o").write_bytes(body)
        (root / "build/a/src/y.c.o").write_bytes(body + root.name.encode())  # differs between the trees
    b = trial.Builder(cfg, tree, reference=clean)
    assert b.unchanged("a", "src/x.c") and not b.unchanged("a", "src/y.c") and not b.unchanged("a", "src/gone.c")
    assert not trial.Builder(cfg, tree).unchanged("a", "src/x.c")  # no reference tree: always scored
    scored = []
    b.score = lambda v, fn, u: scored.append((u, fn)) or 0
    monkeypatch.setattr(check_mod, "exact_fns", lambda c, t, v, units: {"src/x.c": ["f1"], "src/y.c": ["f2"]})
    assert check_mod.broken(b, "a", ["src/x.c", "src/y.c"]) == [] and scored == [("src/y.c", "f2")]


def test_a_diff_that_changes_symbol_rows_re_extracts_the_tree_and_the_tree_is_not_kept(tmp_path, monkeypatch):
    from abu_decomp.data import check as check_mod

    calls = []
    monkeypatch.setattr(
        check_mod, "reextract", lambda profile, tree, files, always=False: calls.append((files, always)) or None
    )
    diff, pruned = setup(tmp_path, monkeypatch, False)
    rows = (
        "diff --git a/versions/us/symbol_addrs.txt b/versions/us/symbol_addrs.txt\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
        "-a\n+b\n"
    )
    diff.write_text(DIFF + rows)
    _r, code, _f = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    # re-extracted for the diff, then back to the clean rows (warm cache): the slot stays reusable, not pruned
    assert code == 0 and calls == [(["include/x.h", "versions/us/symbol_addrs.txt"], False), ([], True)]
    assert ["STAMP-x"] not in pruned


def test_a_stamp_needs_the_rom_check_to_pass(tmp_path, monkeypatch):
    from abu_decomp.data import trial

    diff, _pruned = setup(tmp_path, monkeypatch, False)
    monkeypatch.setattr(trial.Builder, "rom_check", lambda self: (2, "make: *** de ROM differs"))
    _r, code, findings = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    assert code == 1 and findings[0]["key"] == "data.rom" and "de ROM differs" in findings[0]["message"]
    assert not stamp_mod.stamped(tmp_path / "stamps", DIFF)


def test_a_tree_removed_under_the_build_is_named_not_reported_as_a_rom_failure(tmp_path, monkeypatch):
    import shutil

    from abu_decomp.data import trial

    diff, _pruned = setup(tmp_path, monkeypatch, False)

    def gone(self):
        shutil.rmtree(self.tree)
        return 2, "no ROM was linked"

    monkeypatch.setattr(trial.Builder, "rom_check", gone)
    _r, code, findings = stampbuild.build(CFG, None, tmp_path, diff, tmp_path / "stamps")
    assert code == 1 and findings[0]["key"] == "data.stamp" and "vanished during the build" in findings[0]["message"]


def test_rom_check_runs_the_configured_make_arguments_and_is_skipped_without_them(tmp_path):
    from dataclasses import replace

    from abu_decomp.data import trial

    seen = []

    def run(cmd, **k):
        seen.append((cmd, k["cwd"]))
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    cfg = replace(CFG, make=("make",), rom_check=("check", "CHECK=rom"))
    assert trial.Builder(cfg, tmp_path, run=run).rom_check() == (0, "")
    assert seen == [(["nice", "make", "-j1", "check", "CHECK=rom"], tmp_path)]
    assert trial.Builder(CFG, tmp_path, run=run).rom_check() == (0, "") and len(seen) == 1


def test_a_rom_refusal_names_the_version_the_shas_the_offset_and_the_function_and_keeps_the_log(tmp_path):
    import json
    from dataclasses import replace

    from abu_decomp.data import trial

    cfg = replace(
        CFG,
        make=("make",),
        rom_check=("check",),
        rom_image="build/{v}/R.z64",
        rom_base="roms/base.{v}.z64",
        map_file="docs/map-{v}.json",
        versions=("a", "b"),
    )
    for v in ("a", "b"):
        (tmp_path / "build" / v).mkdir(parents=True)
        (tmp_path / "roms").mkdir(exist_ok=True)
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / f"roms/base.{v}.z64").write_bytes(bytes(range(64)))
        (tmp_path / f"build/{v}/R.z64").write_bytes(bytes(range(64)))
        (tmp_path / f"docs/map-{v}.json").write_text(json.dumps({"fns": [["Fn_A", 16, 16, 100.0, "src/a.c", 0]]}))
    bad = bytearray(range(64))
    bad[20] ^= 0xFF
    (tmp_path / "build/b/R.z64").write_bytes(bytes(bad))

    def run(cmd, **k):
        return subprocess.CompletedProcess(cmd, 2, "", "make: *** [Makefile:9: check] Error 1")

    rc, report = trial.Builder(cfg, tmp_path, run=run).rom_check()
    assert rc == 2 and report.startswith("b: expected sha1 ")
    assert "first difference at rom 0x14 (in Fn_A of src/a.c)" in report
    assert "a:" not in report and str(tmp_path / ".stamp-rom.log") in report
    assert "Error 1" in (tmp_path / ".stamp-rom.log").read_text()
    (tmp_path / "build/b/R.z64").write_bytes(bytes(range(64)))  # every ROM matches: say what make did
    again = trial.Builder(cfg, tmp_path, run=run).rom_check()[1]
    assert "make exited 2: make: *** [Makefile:9: check] Error 1" in again


def test_a_make_that_died_without_naming_an_error_is_run_once_more_but_a_real_mismatch_is_not(tmp_path):
    from dataclasses import replace

    from abu_decomp.data import trial

    cfg = replace(CFG, make=("make",), rom_check=("check",))
    runs = []

    def flaky(cmd, **k):
        runs.append(1)
        return subprocess.CompletedProcess(cmd, 139 if len(runs) == 1 else 0, "", "")

    assert trial.Builder(cfg, tmp_path, run=flaky).rom_check() == (0, "") and len(runs) == 2
    runs.clear()

    def dead(cmd, **k):
        runs.append(1)
        return subprocess.CompletedProcess(cmd, 139, "", "")

    rc, report = trial.Builder(cfg, tmp_path, run=dead).rom_check()
    assert rc == 139 and len(runs) == 2 and report.startswith("make exited 139: no output")


def test_setup_runs_one_at_a_time_across_slots(tmp_path):
    import threading
    import time

    inside, peak = [0], [0]

    def work():
        with stampbuild.setup_lock(tmp_path / "code" / "POOL-SRC"):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            time.sleep(0.05)
            inside[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 1
