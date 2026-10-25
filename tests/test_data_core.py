"""decomp data: config, assembly scan, C declarations, layout reader, source edits, literals (all in-memory, no build)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

from abu_decomp import DecompError
from abu_decomp.data import asm, cdecl, clayout, config, literals, source


def test_config_unknown_key_is_ignored_with_a_warning_and_a_missing_config_refused(tmp_path: Path, capsys):
    cfg = config.parse({"nope": 1, "reference": "ref"})  # a key from a newer config than this tool
    assert cfg.reference == "ref" and "ignoring unknown key(s) nope" in capsys.readouterr().err
    with pytest.raises(DecompError, match="no data config"):
        config.load(None, tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "data.toml").write_text('[data]\nreference = "a"\nversions = ["a", "b"]\n')
    cfg = config.load(None, tmp_path)
    assert cfg.versions == ("a", "b") and cfg.define("a-b") == "-DVERSION_A_B"


ASM = """\
/* 0 80000000 3C018012 */ lui $at, %hi(D_80120000)
/* 4 80000004 8C2A0000 */ lw $t2, %lo(D_80120000)($at)
/* 8 80000008 3C0B8012 */ lui $t3, %hi(D_80120010)
/* C 8000000C 256B0010 */ addiu $t3, $t3, %lo(D_80120010)
/* 10 80000010 8D6C0004 */ lw $t4, 4($t3)
/* 14 80000014 E56D0008 */ swc1 $f4, 8($t3)
"""


def test_scan_fn_direct_and_base_offset():
    acc: dict = defaultdict(list)
    asm.scan_fn(ASM, acc, "fn")
    assert ("fn", 0, 4, "s32", "L") in acc["D_80120000"]
    kinds = {(r[1], r[3], r[4]) for r in acc["D_80120010"]}
    assert (4, "s32", "L") in kinds and (8, "f32", "S") in kinds and (0, "", "A") in kinds


def test_declarators_and_kinds():
    tok = re.compile(r"\bD_[0-9A-F]{8}\w*")
    assert cdecl.declarators("s32 D_80000000, *D_80000004, D_80000008[4]", tok) == [
        ("D_80000000", "s32"),
        ("D_80000004", "s32 *"),
        ("D_80000008", "s32[4]"),
    ]
    assert (
        cdecl.decl_kind(["f32"]) == {"f"} and cdecl.decl_kind(["Foo[2]"]) == {"a"} and cdecl.decl_kind(["Foo"]) == {"s"}
    )


def test_load_cdecls_externs_uses_and_local_defs(tmp_path: Path):
    cfg = config.parse({"reference": "r", "versions": ["r"]})
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include" / "A.H").write_text("extern Foo D_80000000;\n/* extern s32 D_80000099; */\n")
    (tmp_path / "src" / "a.c").write_text("Bar D_80000010;\nvoid f(void) { D_80000000.x = 1; }\n")
    decl, uses, local = cdecl.load_cdecls(tmp_path, cfg)
    assert decl["D_80000000"] == [("include/A.H", "Foo")] and "D_80000099" not in decl
    assert local["D_80000010"] == [("src/a.c", "Bar")] and uses["D_80000000"] == {"include/A.H", "src/a.c"}


HDR = """
#define N 3
typedef unsigned char u8; typedef signed short s16; typedef int s32; typedef float f32;
typedef struct Foo Foo;
typedef struct { s32 a; u8 b[N]; } Inner;
struct Foo { f32 x; Inner in[2]; union { s32 w; s16 h[2]; } u; s32 *p; u8 pad[0x10 - sizeof(Inner)]; };
"""


def test_layout_size_forward_typedef_and_sizeof_dim():
    t = clayout.Types()
    t.add_text(HDR)
    foo = t.parse_type_text("Foo")
    assert [(n, o) for n, _, o in foo.members][:4] == [("x", 0), ("in", 4), ("u", 20), ("p", 24)]
    assert foo.size == 36
    assert t.parse_type_text("Foo[3]").size == 3 * foo.size


def test_leaf_path_member_array_subword_padding():
    t = clayout.Types()
    t.add_text(HDR)
    foo = t.parse_type_text("Foo")
    assert clayout.leaf_path(foo, 0, 4, True) == (".x", "")
    assert clayout.leaf_path(foo, 12, 4, False) == (".in[1].a", "")
    assert clayout.leaf_path(foo, 16, 1, None) == (".in[1].b[0]", "")
    assert clayout.leaf_path(foo, 13, 1, None) == (".in[1].a", "subword")
    assert clayout.leaf_path(foo, 28, 1, None)[1] == "padding"
    assert clayout.leaf_path(foo, 1000, 4, None) is None


def test_closure_reads_only_included_headers(tmp_path: Path):
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "T.H").write_text("typedef int s32;\n")
    (inc / "A.H").write_text('#include "T.H"\ntypedef struct { s32 q; } S;\n')
    (inc / "B.H").write_text("typedef struct { char q[9]; } S;\n")
    t = clayout.read_closure(inc, "A.H")
    assert t.parse_type_text("S").size == 4


def test_source_rewrite_drops_externs_and_adds_include():
    text = (
        '#include "X.H"\nextern s32 D_80000000, D_80000004;\nextern f32 D_80000008;\nvoid f(void) {\n'
        '    D_80000000 = 1; /* D_80000000 */\n    g(&D_80000000, "D_80000000");\n}\n'
    )
    out = source.rewrite_file(text, "D_80000000", "D_8011FAC0.ring.current", "COMMON/GAME_SESSION.H")
    assert "extern s32 D_80000004;" in out and "extern s32 D_80000000" not in out
    assert "D_8011FAC0.ring.current = 1; /* D_80000000 */" in out
    assert 'g(&D_8011FAC0.ring.current, "D_80000000");' in out
    assert '#include "COMMON/GAME_SESSION.H"' in out
    only = source.rewrite_file("extern s32 D_80000000;\n", "D_80000000", "O.x", "O.H")
    assert "D_80000000" not in only


def test_put_back_restores_one_function_and_declares():
    text = '#include "X.H"\nvoid a(void) {\n    O.x = 1;\n}\nvoid b(void) {\n    O.x = 2;\n}\n'
    out = source.put_back(text, "b", "O.x", "D_80000000", "s32")
    assert "O.x = 1;" in out and "D_80000000 = 2;" in out and "extern s32 D_80000000;" in out
    assert source.put_back(out, "a", "O.xy", "D_80000000", "s32") == out


def test_unwrap_and_body_span():
    src = '#ifdef NON_MATCHING\nint f(void) {\n    return 1;\n}\n#else\nINCLUDE_ASM("x", f);\n#endif\n'
    out = source.unwrap(src, "f")
    assert "INCLUDE_ASM" not in out and source.body_span(out, "f")


def test_float_literals_roundtrip():
    assert literals.float_literal(bytes.fromhex("3F800000"), "f32") == "1.0f"
    assert literals.float_literal(bytes.fromhex("3F7D70A4"), "f32") == "0.99f"
    assert literals.float_literal(bytes.fromhex("3FF0000000000000"), "f64") == "1.0"
    assert literals.float_literal(b"\x00", "f32") is None
    assert literals.literal_for({"literal": True, "init": "3f800000", "access": {"4:f32": 2}}) == "1.0f"
    assert literals.literal_for({"literal": False, "init": "3f800000", "access": {"4:f32": 2}}) is None
    assert literals.drop_dead_externs("extern f32 D_1, D_2;\nint x;\n", ["D_1"]) == "extern f32 D_2;\nint x;\n"
