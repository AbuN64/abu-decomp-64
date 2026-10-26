"""TOOL-SPEED-2 (r)/(t): `decomp data check` reaches every unit naming a changed symbol, in every version, and a unit
that does not compile does not hide the others."""

import contextlib
import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from abu_decomp.data import check, trial
from abu_decomp.data.config import DataConfig

CFG = DataConfig(reference="a", versions=("a", "b"))


def done(out="", code=0):
    return subprocess.CompletedProcess([], code, out, "")


def test_changed_symbols_come_from_the_changed_lines_only():
    diff = "+++ b/x.c\n-extern u8 D_800C0000;\n+extern Foo D_800C0000;\n context D_800C9999\n+int y = D_800C1234_de;\n"
    run = lambda *a, **k: done(diff)  # noqa: E731
    assert check.changed_symbols(CFG, Path("."), run) == ["D_800C0000", "D_800C1234_de"]


def test_every_unit_naming_a_symbol_is_found_not_only_the_changed_ones(tmp_path):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return done("src/a/u1.CPP\nsrc/a/u2.c\ninclude/h.H\nsrc/a/readme.txt\n")

    assert check.referencing_units(CFG, tmp_path, ["D_1"], run) == ["src/a/u1.CPP", "src/a/u2.c"]
    assert "-w" in seen[0][1] or "-rlwF" in seen[0]
    assert check.referencing_units(CFG, tmp_path, [], run) == []


def test_units_a_version_does_not_build_are_left_out(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/map-b.json").write_text(json.dumps({"fns": [["f", 0, 0, 100, "src/a/u1.CPP"]]}))
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": [["g", 0, 0, 100, "src/a/de_only.CPP"]]}))
    assert check.in_version(CFG, tmp_path, "b", ["src/a/u1.CPP", "src/a/de_only.CPP"]) == ["src/a/u1.CPP"]
    assert check.in_version(CFG, tmp_path, "zz", ["src/a/u1.CPP"]) == ["src/a/u1.CPP"]  # no map: nothing to judge by


def test_data_only_units_build_in_every_version_and_build_errors_block(tmp_path):
    (tmp_path / "docs").mkdir()
    for version in CFG.versions:
        (tmp_path / f"docs/map-{version}.json").write_text(json.dumps({"fns": []}))
    seen = []
    failed = False

    def objects(version, units, keep_going=False):
        seen.append((version, units))
        if failed:
            return 2, f"make: *** No rule to make target 'build/{version}/src/data.c.o'.  Stop."
        return 0, ""

    builder = SimpleNamespace(cfg=CFG, tree=tmp_path, run=lambda *a, **k: done(), objects=objects)
    for version in CFG.versions:
        result = check.check(builder, version, ["src/data.c"])
        assert result["units"] == 1 and result["built"] and result["broken"] == []
    assert seen == [(v, ["src/data.c"]) for v in CFG.versions]
    failed = True
    result = check.check(builder, "a", ["src/data.c"])
    assert result["units"] == 1 and not result["built"]
    assert result["errors"] == ["make: *** No rule to make target 'build/a/src/data.c.o'.  Stop."]  # never empty
    builder.objects = lambda version, units, keep_going=False: (2, "")
    assert check.check(builder, "a", ["src/data.c"])["errors"] == ["make exited 2 with no output"]


def test_a_failing_unit_is_named_with_its_error_and_the_rest_still_scored(tmp_path):
    (tmp_path / "docs").mkdir()
    rows = [["f1", 0, 0, 100, "src/a/ok.CPP"], ["f2", 0, 0, 100, "src/a/bad.CPP"]]
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": rows}))
    log = (
        "src/a/bad.CPP:3: error: conflicting types for 'D_1'\nmake: *** [Makefile:1: build/a/src/a/bad.CPP.o] Error 1\n"
    )
    builder = SimpleNamespace(
        cfg=CFG,
        tree=tmp_path,
        run=lambda *a, **k: done(),
        objects=lambda v, u, keep_going=False: (2, log),
        score=lambda v, fn, unit: 0 if fn == "f1" else 7,
    )
    res = check.check(builder, "a", ["src/a/ok.CPP", "src/a/bad.CPP"])
    assert res["built"] is False and res["failed"] == [
        ["src/a/bad.CPP", "src/a/bad.CPP:3: error: conflicting types for 'D_1'"]
    ]
    assert res["broken"] == []  # f2 is in the failed unit: not scored; f1 built and matches


def test_a_unit_failing_as_it_did_before_the_change_is_preexisting(tmp_path):
    (tmp_path / "docs").mkdir()
    rows = [["f1", 0, 0, 100, "src/a/ok.CPP"], ["f2", 0, 0, 100, "src/a/old.CPP"], ["f3", 0, 0, 100, "src/a/new.CPP"]]
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": rows}))
    log = "src/a/old.CPP:1: error: old\nmake: *** [Makefile:1: build/a/src/a/old.CPP.o] Error 1\n"
    log += "src/a/new.CPP:3: error: mine\nmake: *** [Makefile:1: build/a/src/a/new.CPP.o] Error 1\n"
    builder = SimpleNamespace(
        cfg=CFG,
        tree=tmp_path,
        run=lambda *a, **k: done(),
        objects=lambda v, u, keep_going=False: (2, log),
        score=lambda v, fn, unit: 0,
    )
    base = {"src/a/old.CPP": "src/a/old.CPP:1: error: old", "src/a/new.CPP": "src/a/new.CPP:3: error: other"}
    res = check.check(builder, "a", ["src/a/ok.CPP", "src/a/old.CPP", "src/a/new.CPP"], base)
    assert res["preexisting"] == [["src/a/old.CPP", "src/a/old.CPP:1: error: old"]]
    assert [u for u, _ in res["failed"]] == ["src/a/new.CPP"] and res["built"] is False
    assert set(res["all_failed"]) == {"src/a/old.CPP", "src/a/new.CPP"}


def test_a_failed_intermediate_rule_still_names_its_unit():
    log = "src/a/bad.CPP:477:2: error: #error x\nmake: *** [Makefile:446: build/a/src/a/bad.CPP.i.ok] Error 1\n"
    assert check.failures(SimpleNamespace(cfg=CFG), "a", ["src/a/bad.CPP", "src/a/ok.CPP"], log) == {
        "src/a/bad.CPP": "src/a/bad.CPP:477:2: error: #error x"
    }


def test_since_judges_a_committed_diff_not_the_index(tmp_path):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return done("+int y = D_800C1234_de;\n" if "-U0" in cmd else "src/a/u1.CPP\n")

    assert check.changed_symbols(CFG, tmp_path, run, base="abc") == ["D_800C1234_de"]
    assert seen[0][-1] == "abc"
    assert check.changed_units(tmp_path, run, "", "abc") == ["src/a/u1.CPP"]
    assert seen[1][-1] == "abc"


def test_a_function_the_diff_renames_is_scored_by_its_new_name(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": [["func_80001000", 0, 0, 100, "src/a/u.CPP"]]}))
    sym = "-func_80001000 = 0x80001000;\n+Math_Sqrtf = 0x80001000;\n-other = 0x80002000;\n+Other = 0x80003000;\n"
    seen = []

    def run(cmd, **kw):
        return done(sym if "symbol_addrs" in cmd[-1] else "")

    def score(v, fn, unit):
        seen.append(fn)
        return 0 if fn == "Math_Sqrtf" else None

    builder = SimpleNamespace(
        cfg=CFG, tree=tmp_path, run=run, objects=lambda v, u, keep_going=False: (0, ""), score=score
    )
    res = check.check(builder, "a", ["src/a/u.CPP"])
    assert seen == ["Math_Sqrtf"] and res["broken"] == []


def test_reextract_skips_a_current_tree_and_refuses_a_tree_moved_over_splat_changes(tmp_path, monkeypatch):
    from abu_decomp import tools

    calls = []
    monkeypatch.setattr(tools, "invoke", lambda *a, **k: calls.append(a) or SimpleNamespace(ok=True))
    (tmp_path / ".unbake").mkdir()
    state = {"ls-files": "versions/us/symbol_addrs.txt\nsrc/a.c\n", "diff": "", "rev-parse": "H1\n"}

    def run(cmd, **kw):
        return done(state[cmd[3]])

    (tmp_path / "versions/us").mkdir(parents=True)
    (tmp_path / "versions/us/symbol_addrs.txt").write_text("a = 0x1;\n")
    assert check.reextract(None, tmp_path, [], always=True, run=run) is None and len(calls) == 1
    assert check.reextract(None, tmp_path, [], always=True, run=run) is None and len(calls) == 1  # same inputs: skipped
    (tmp_path / "versions/us/symbol_addrs.txt").write_text("b = 0x1;\n")
    state["diff"] = "versions/us/splat.yaml\n"
    why = check.reextract(None, tmp_path, [], always=True, run=run)
    assert why and "splat.yaml" in why and "--lease" in why and len(calls) == 1
    assert check.reextract(None, tmp_path, [], always=True, allow=True, run=run) is None and len(calls) == 2
    state["diff"] = "versions/us/symbol_addrs.txt\n"
    (tmp_path / "versions/us/symbol_addrs.txt").write_text("c = 0x1;\n")
    assert (
        check.reextract(None, tmp_path, [], always=True, run=run) is None and len(calls) == 3
    )  # a rename-only move is cheap


def test_a_tree_make_extract_re_extracted_is_not_moved_over_splat_changes(tmp_path):
    import hashlib

    (tmp_path / "versions/us").mkdir(parents=True)
    (tmp_path / "versions/us/splat.yaml").write_text("new\n")
    (tmp_path / "versions/us/symbol_addrs.txt").write_text("a = 0x1;\n")
    run = lambda cmd, **kw: done("versions/us/splat.yaml\n")  # noqa: E731
    mark = {"head": "H0"}
    assert check._moved_splat(tmp_path, mark, run) == ["versions/us/splat.yaml"]  # nothing recorded the extraction
    (tmp_path / "build").mkdir()
    key = hashlib.sha1(b"new\na = 0x1;\n").hexdigest()
    (tmp_path / "build/.extract-src-us").write_text(key + "\n")  # what `make extract` writes
    assert check._moved_splat(tmp_path, mark, run) == []
    (tmp_path / "versions/us/splat.yaml").write_text("newer\n")  # edited since: really moved
    assert check._moved_splat(tmp_path, mark, run) == ["versions/us/splat.yaml"]


def test_a_fn_that_moved_to_another_unit_is_scored_by_symbol(tmp_path):
    (tmp_path / "docs").mkdir()
    rows = [["f1", 0, 0, 100, "src/a/asm_unit.CPP"], ["f2", 0, 0, 100, "src/a/asm_unit.CPP"]]
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": rows}))
    where = {
        ("f1", "src/a/c_unit.CPP"): 0,
        ("f2", "src/a/c_unit.CPP"): 5,
    }  # both became C in c_unit; asm_unit no longer holds them
    builder = SimpleNamespace(
        cfg=CFG,
        tree=tmp_path,
        run=lambda *a, **k: done(),
        score=lambda v, fn, unit: where.get((fn, unit)),
    )
    bad = check.broken(builder, "a", ["src/a/asm_unit.CPP", "src/a/c_unit.CPP"])
    assert bad == [("src/a/asm_unit.CPP", "f2", "edit=5")]  # f1 matched by symbol; f2 is really broken, not "no-score"


def test_a_rename_already_staged_in_the_tree_is_still_the_diffs_rename(tmp_path):
    """`git diff` alone shows only unstaged changes: a lane that staged part of its rename (REV1-ONE-30: MM files) had
    that part scored under the old names (func_80274098_de edit=67). The working diff is against HEAD."""
    import subprocess as sp

    def git(*a):
        sp.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "versions").mkdir()
    sym = tmp_path / "versions/symbol_addrs.txt"
    sym.write_text("func_80001000 = 0x80001000;\nfunc_80002000 = 0x80002000;\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    sym.write_text("Math_Sqrtf = 0x80001000;\nfunc_80002000 = 0x80002000;\n")
    git("add", "-A")  # staged
    sym.write_text("Math_Sqrtf = 0x80001000;\nOther_Name = 0x80002000;\n")  # and one more unstaged
    builder = SimpleNamespace(tree=tmp_path)
    assert check.renamed(builder) == {"func_80001000": "Math_Sqrtf", "func_80002000": "Other_Name"}
    assert check.changed_files(tmp_path) == ["versions/symbol_addrs.txt"]


def test_every_exact_function_of_a_version_is_scored_by_one_fncmp_batch(tmp_path):
    """The check asks a version's functions in one process (284 s wall for ~4000 single calls on a header edit)."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "-C"] or "--batch" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        names = [line.split()[0] for line in kw["input"].splitlines()]
        return subprocess.CompletedProcess(
            cmd, 0, "".join(f"{n} ALIGNED edit={0 if n != 'f3' else 4}\n" for n in names), ""
        )

    cfg = dataclasses.replace(CFG, fncmp_batch=("fncmp", "--batch", "{v}"), fncmp=("fncmp", "{v}", "{fn}", "{obj}"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/map-us.json").write_text(
        json.dumps({"fns": [["f1", 0, 0, 100, "src/a.c"], ["f2", 0, 0, 100, "src/a.c"], ["f3", 0, 0, 100, "src/b.c"]]})
    )
    b = trial.Builder(cfg, tmp_path, run=run)
    assert check.broken(b, "us", ["src/a.c", "src/b.c"]) == [("src/b.c", "f3", "edit=4")]
    assert len([c for c in calls if "--batch" in c]) == 1
    assert not [c for c in calls if c[:1] == ["fncmp"] and "--batch" not in c]  # no single call


def test_reextract_runs_setup_under_the_exclusive_tree_lock_a_build_holds(tmp_path, monkeypatch):
    from unbake import treelock

    from abu_decomp import tools

    held = []

    @contextlib.contextmanager
    def hold(tree, exclusive=False, who="", **kw):
        held.append((tree, exclusive, who))
        yield 0.0

    monkeypatch.setattr(treelock, "hold", hold)
    monkeypatch.setattr(tools, "invoke", lambda *a, **k: held.append("setup") or SimpleNamespace(ok=True))
    (tmp_path / ".unbake").mkdir()
    (tmp_path / "versions/us").mkdir(parents=True)
    (tmp_path / "versions/us/symbol_addrs.txt").write_text("a = 0x1;\n")
    run = lambda cmd, **kw: done({"ls-files": "versions/us/symbol_addrs.txt\n", "diff": "", "rev-parse": "H\n"}[cmd[3]])  # noqa: E731
    assert check.reextract(None, tmp_path, [], always=True, run=run) is None
    assert held == [(tmp_path, True, "data check setup"), "setup"]


def test_a_renamed_source_file_is_built_and_scored_under_its_new_path(tmp_path):
    """A folder rename: the map and the build's .d files still name the old path (stamp of a 132-file rename: every
    function of every moved unit was "no-score")."""
    (tmp_path / "docs").mkdir()
    rows = [["f1", 0, 0, 100, "src/old/u.CPP"], ["f2", 0, 0, 100, "src/gone/x.CPP"]]
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": rows}))
    status = "R098\tsrc/old/u.CPP\tsrc/new/u.CPP\nD\tsrc/gone/x.CPP\nM\tsrc/a/m.CPP\n"
    built, scored = [], []

    def run(cmd, **kw):
        return done(status if "--name-status" in cmd else "")

    def objects(v, u, keep_going=False):
        built.append(list(u))
        return 0, ""

    def score(v, fn, unit):
        scored.append((fn, unit))
        return 0

    builder = SimpleNamespace(cfg=CFG, tree=tmp_path, run=run, objects=objects, score=score)
    res = check.check(builder, "a", ["src/old/u.CPP", "src/gone/x.CPP"])  # the stale .d names both old paths
    assert built == [["src/new/u.CPP"]] and scored == [("f1", "src/new/u.CPP")] and res["broken"] == []


def test_data_check_brings_a_stale_extract_current_instead_of_refusing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from abu_decomp.data import command

    seen = {}

    def fake(profile, tree, files, always=False, allow=False, run=None):
        seen["allow"] = allow
        return "stop here"

    monkeypatch.setattr(command.check_mod, "reextract", fake)
    monkeypatch.setattr(command.check_mod, "changed_files", lambda *a, **k: [])
    cli = SimpleNamespace(_profile=lambda p: None)
    (tmp_path / "build").mkdir()
    (tmp_path / "build/.pool-extracted-at").write_text("abc\n")
    args = SimpleNamespace(profile=None, tree=tmp_path, since=None, reextract=False, unit=[])
    command._check(args, None, None, cli)
    assert seen["allow"] is True


def test_prepare_signs_every_versions_narrowed_includers_before_any_build(monkeypatch, tmp_path):
    """The first make's stamp sweep covers every version: the stamps the narrowing proved are signed for all versions
    before check() builds one, and check() builds the planned units (not the narrowed ones)."""
    from unbake import hdrnarrow

    plans = {
        "a": {"units": ["src/x.c"], "narrowed": {"a": {"skipped": 2}}, "skipped": ["src/y.c", "src/z.c"]},
        "b": {"units": ["src/x.c"], "narrowed": {}, "skipped": []},
    }
    monkeypatch.setattr(check, "plan", lambda b, v: plans[v])
    signed = []
    monkeypatch.setattr(hdrnarrow, "sign", lambda root, stamps: signed.extend(stamps) or len(stamps))
    b = SimpleNamespace(tree=tmp_path, cfg=CFG, run=None)
    check.prepare(b, ["a", "b"])
    assert signed == ["build/a/src/y.c.i.ok", "build/a/src/z.c.i.ok"]
    assert b.plans["a"]["narrowed"]["a"]["signed"] == 2
    built = []
    b.objects = lambda v, units, keep_going=False: built.append((v, units)) or (0, "")
    monkeypatch.setattr(check, "failures", lambda b, v, units, log: {})
    monkeypatch.setattr(check, "broken", lambda b, v, units, ren, moved: [])
    monkeypatch.setattr(check, "renamed", lambda b: {})
    monkeypatch.setattr(check, "moved_units", lambda tree, base, run: {})
    monkeypatch.setattr(check, "in_version", lambda cfg, tree, v, units: units)
    got = check.check(b, "a")
    assert built == [("a", ["src/x.c"])] and got["narrowed"]["signed"] == 2


def test_a_unit_the_version_already_built_stays_though_only_other_maps_list_it(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/map-b.json").write_text(json.dumps({"fns": [["f", 0, 0, 100, "src/a/u1.CPP"]]}))
    (tmp_path / "docs/map-a.json").write_text(json.dumps({"fns": [["g", 0, 0, 100, "src/a/shared.CPP"]]}))
    units = ["src/a/u1.CPP", "src/a/shared.CPP"]
    assert check.in_version(CFG, tmp_path, "b", units) == ["src/a/u1.CPP"]
    obj = tmp_path / CFG.object.format(v="b", unit="src/a/shared.CPP")
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"")
    assert check.in_version(CFG, tmp_path, "b", units) == units


def test_a_tree_without_the_pool_marker_is_refused_naming_the_marker_and_the_fix(tmp_path):
    from abu_decomp.data import command

    why = check.marker_refusal(tmp_path)
    assert why and ".pool-extracted-at" in why and "worktree new" in why and "--lease" in why
    result, code, findings = command._check(SimpleNamespace(unit=[], tree=tmp_path), CFG, None, None)
    assert (
        code == 1
        and result == {}
        and findings[0]["key"] == "data.marker"
        and ".pool-extracted-at" in findings[0]["message"]
    )


def test_a_marked_tree_is_not_refused(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build/.pool-extracted-at").write_text("abc\n")
    assert check.marker_refusal(tmp_path) is None
    (tmp_path / "build/.pool-extracted-at").unlink()
    (tmp_path / "build/.pool-built-at").write_text("abc\n")
    assert check.marker_refusal(tmp_path) is None
