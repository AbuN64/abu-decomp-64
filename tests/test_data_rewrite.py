"""TOOL-SPEED-5 (t): `decomp data rewrite` keeps the raw symbol where the rewrite would re-type or mis-index it."""

from abu_decomp.data import rewrite, source
from abu_decomp.data.config import DataConfig

HDR = "world.h"


def plan(text, sym="D_1", expr="D_2.m", kinds=None, access=None, decl=None):
    return source.plan_file(
        text, sym, expr, HDR, False, {"decl_kind": sorted(kinds), "access": access, "decl": decl or []}
    )


def test_plain_scalar_file_is_rewritten_and_gets_the_owner_include():
    new, why = plan('#include "a.h"\nextern s32 D_1;\nint f(void) { return D_1; }\n', kinds={"i"})
    assert why == "" and "return D_2.m;" in new and "extern s32 D_1" not in new and '#include "world.h"' in new


def test_unit_local_struct_alias_keeps_the_raw_symbol():
    text = "extern struct Pair D_1;\nint f(void) { return D_1.a; }\n"
    new, why = plan(text, kinds={"i", "s"})
    assert new == text and "struct alias" in why


def test_unit_local_definition_is_skipped():
    text = "static s32 D_1 = 3;\nint f(void) { return D_1; }\n"
    assert "unit-local definition" in plan(text, kinds={"i"})[1]


def test_array_symbol_uses_the_whole_member_not_its_first_element():
    text = "extern char *D_1[];\nchar *f(int i) { return D_1[i]; }\n"
    new, why = plan(text, expr="D_2.registered[0]", kinds={"a"})
    assert why == "" and "D_2.registered[i]" in new and "registered[0][i]" not in new
    header_only = "char *f(int i) { return D_1[i]; }\n"  # declared by an included header as an array
    assert "D_2.registered[i]" in plan(header_only, expr="D_2.registered[0]", kinds={"p"}, decl=["char *[]"])[0]
    scalar = "extern char *D_1;\nchar *f(void) { return D_1; }\n"
    assert "D_2.registered[0];" in plan(scalar, expr="D_2.registered[0]", kinds={"p"})[0]


def test_declared_kind_against_rom_access_is_a_mismatch():
    text = "extern s32 D_1;\nint f(void) { return D_1; }\n"
    new, why = plan(text, kinds={"i"}, access={"4:f32": 9})
    assert new == text and "ROM accesses it as f" in why
    assert plan(text, kinds={"i"}, access={"4:s32": 9})[1] == ""


def test_owner_declared_locally_gets_no_second_include_and_a_scalar_owner_skips_the_file():
    ok = "extern struct W D_2;\nextern s32 D_1;\nint f(void) { return D_1; }\n"
    new, why = plan(ok, kinds={"i"})
    assert why == "" and '#include "world.h"' not in new and "D_2.m" in new
    bad = "extern char D_2[];\nextern s32 D_1;\nint f(void) { return D_1; }\n"
    assert "owner D_2 is declared here" in plan(bad, kinds={"i"})[1]


def test_without_the_census_member_the_old_unguarded_rewrite_still_runs(tmp_path):
    (tmp_path / "src").mkdir()
    f = tmp_path / "src/a.c"
    f.write_text("extern struct Pair D_1;\nint f(void) { return D_1.a; }\n")
    cfg = DataConfig(reference="a", versions=("a",), source_dirs=("src",), source_suffixes=(".c",))
    skipped = {}
    assert rewrite.run(cfg, tmp_path, "D_1", "D_2.m", HDR, dry=True) == ["src/a.c"]
    assert (
        rewrite.run(cfg, tmp_path, "D_1", "D_2.m", HDR, dry=True, member={"decl_kind": ["i", "s"]}, skipped=skipped)
        == []
    )
    assert list(skipped) == ["src/a.c"]


def test_a_2d_array_declaration_is_refused_not_mis_indexed():
    text = "extern s32 D_1[4][2];\nint f(int i) { return D_1[i][1]; }\n"
    new, why = plan(text, expr="D_2.cells[0]", kinds={"i"})
    assert new == text and "2-D array" in why


def test_a_1d_array_against_an_array_of_arrays_member_is_refused():
    text = "extern s32 D_1[8];\nint f(int i) { return D_1[i]; }\n"
    new, why = plan(text, expr="D_2.rows[0][0]", kinds={"i"})
    assert new == text and "array of arrays" in why
    ok, why = plan(text, expr="D_2.rows[0]", kinds={"i"})
    assert why == "" and "D_2.rows" in ok
