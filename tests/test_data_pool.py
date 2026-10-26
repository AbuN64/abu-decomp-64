"""decomp data: multi-function place, version-guarded literal apply, and an unproved pool word refused by name (all mocked)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from abu_decomp import DecompError, cli, tools
from abu_decomp.data import command, gate, literals, rewrite
from abu_decomp.data.config import DataConfig

CFG = DataConfig(reference="us", versions=("us", "de"))


def lit(sym, addr, init, size=None, width="4:f32"):
    return {
        "sym": sym, "addr": addr, "init": init, "size": size or len(init) // 2, "literal": True,
        "access": {width: 1}, "class": "LITERAL", "decl_kind": ["f"], "decl": ["f32"],
    }  # fmt: skip


def census(*lones):
    return {"clusters": [], "lones": list(lones)}


DIRECTION = census(
    lit("D_800C53B0_de", "0x800C53B0", "3f8000003f000000"),  # two words; the middle one is read through its own label
    lit("D_800C53B8_de", "0x800C53B8", "3f000000"),
)


def test_place_passes_one_place_per_function_and_lists_rows_for_each(monkeypatch):
    seen = []
    rows = [
        {"function": "func_a", "version": "us", "row": "literals_func_a", "file": "src/a.c"},
        {"function": "func_b", "version": "us", "row": "literals_func_b", "file": "src/b.c"},
    ]

    def invoke(tool, profile, argv, **kw):
        seen.append((tool, list(argv)))
        return SimpleNamespace(ok=True, document={"result": {"placed": rows}})

    monkeypatch.setattr(tools, "invoke", invoke)
    monkeypatch.setattr(cli, "_profile", lambda p: None)
    args = SimpleNamespace(profile=None, tree=Path("."), check=True, function=["func_a", "func_b", "func_c"])
    result, code, findings = command._place(args)
    assert seen == [("unbake", ["sync", "--place", "func_a", "--place", "func_b", "--place", "func_c", "--check"])]
    assert code == 0 and not findings
    assert result["lines"][:2] == ["func_a us: literals_func_a (src/a.c)", "func_b us: literals_func_b (src/b.c)"]
    assert "func_c: no row to place" in result["lines"]  # an empty plan never reads as validated


def test_probe_apply_literals_finds_a_version_guarded_draft_the_map_does_not_list(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "map-us.json").write_text(json.dumps({"fns": []}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "draft.c").write_text(
        "#if VERSION_US\nextern f32 D_800C91F8;\nvoid func_8025F074_us(void) {\n    x += D_800C91F8;\n}\n"
        "#elif VERSION_DE\nextern f32 D_800C91F8_de;\nvoid func_8025F0D4_de(void) {\n    x += D_800C91F8_de;\n}\n#endif\n"
    )
    cen = tmp_path / "census.json"
    cen.write_text(
        json.dumps(census(lit("D_800C91F8", "0x800C91F8", "3f800000"), lit("D_800C91F8_de", "0x800C91F8", "3f800000")))
    )
    args = SimpleNamespace(
        census=cen, tree=tmp_path, version=None, function=["func_8025F074_us", "func_8025F0D4_de"],
        apply_literals=True, edit=None,
    )  # fmt: skip
    result, code, _ = command._probe(args, CFG)
    assert code == 0 and not any("NO-UNIT" in line for line in result["lines"])
    text = (tmp_path / "src" / "draft.c").read_text()
    assert "x += 1.0f;" in text and "D_800C91F8" not in text.split("#elif")[0].split("\n", 2)[2]


def test_a_record_with_an_unlisted_interior_word_is_named():
    assert literals.unlisted_interior(DIRECTION) == {"D_800C53B0_de": ["D_800C53B4_de"]}
    listed = census(*DIRECTION["lones"], lit("D_800C53B4_de", "0x800C53B4", "3f000000"))
    assert literals.unlisted_interior(listed) == {}
    wide = census(lit("D_800C2000", "0x800C2000", "3ff0000000000000", width="8:f64"))
    assert literals.unlisted_interior(wide) == {}  # an eight-byte double reads all eight bytes


def test_rewrite_refuses_the_unproved_record_before_touching_a_file(tmp_path):
    cen = tmp_path / "census.json"
    cen.write_text(json.dumps(DIRECTION))
    args = SimpleNamespace(census=cen, symbol="D_800C53B0_de", expr="0.25f", header="TYPES.H", only=None, dry=False,
                           mixed=False, version=None)  # fmt: skip
    with pytest.raises(DecompError, match="D_800C53B4_de"):
        command._rewrite(args, CFG)
    with pytest.raises(DecompError, match="D_800C53B4_de"):
        rewrite.refuse_interior(DIRECTION, "D_800C53B0_de")
    rewrite.refuse_interior(DIRECTION, "D_800C53B8_de")  # a proved record passes


def test_apply_literals_refuses_a_function_reading_the_unproved_record(tmp_path):
    (tmp_path / "src").mkdir()
    src = "extern f32 D_800C53B0_de;\nvoid func_x_de(void) {\n    y = D_800C53B0_de;\n}\n"
    (tmp_path / "src" / "a.c").write_text(src)
    line = command._apply_literals(CFG, tmp_path, DIRECTION, "func_x_de", "src/a.c")
    assert "REFUSED" in line and "D_800C53B4_de" in line
    assert (tmp_path / "src" / "a.c").read_text() == src


def test_check_diff_refuses_a_diff_dropping_the_unproved_record(tmp_path):
    cen = tmp_path / "census.json"
    cen.write_text(json.dumps(DIRECTION))
    diff = (
        "--- a/src/a.c\n+++ b/src/a.c\n@@ -1,2 +1,1 @@\n-extern f32 D_800C53B0_de;\n-y = D_800C53B0_de;\n+y = 0.25f;\n"
    )
    bad = gate.check_diff(CFG, cen, diff)
    assert len(bad) == 1 and "D_800C53B4_de" in bad[0]
    assert gate.check_diff(CFG, cen, diff.replace("D_800C53B0_de", "D_800C53B8_de")) == []
