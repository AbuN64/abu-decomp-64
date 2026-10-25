"""TOOL-SPEED-2 (r)/(t): `decomp data check` reaches every unit naming a changed symbol, in every version, and a unit
that does not compile does not hide the others."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from abu_decomp.data import check
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
    assert check.in_version(CFG, tmp_path, "b", ["src/a/u1.CPP", "src/a/de_only.CPP"]) == ["src/a/u1.CPP"]
    assert check.in_version(CFG, tmp_path, "zz", ["src/a/u1.CPP"]) == ["src/a/u1.CPP"]  # no map: nothing to judge by


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
