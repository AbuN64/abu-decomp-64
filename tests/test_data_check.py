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
