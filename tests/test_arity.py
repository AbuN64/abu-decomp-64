from __future__ import annotations

import re
from pathlib import Path

from abu_decomp import arity

SYM = re.compile(r"func_[0-9A-Fa-f]{8}\w*")

DEF = "void func_80209CD8_de(SharedPlayer **arg0) {\n    int x = 1;\n}\n"
USER = (
    "extern void func_80209CD8_de(Brain *, Player *);\nvoid g(void) {\n"
    "    if (a) func_80209CD8_de(brain, player);\n    (void)func_80209CD8_de(b, c);\n}\n"
)


def test_rows_tell_definitions_prototypes_and_calls():
    rows = arity.rows_of(DEF + USER, SYM)
    assert [(r[1], r[3]) for r in rows] == [("d", 1), ("p", 2), ("c", 2), ("c", 2)]
    assert rows[0][4] == ["SharedPlayer **arg0"]


def test_rows_ignore_comments_strings_macros_and_void():
    text = (
        "/* func_80209CD8_de(1) */ // func_80209CD8_de(2)\n#define M func_80209CD8_de(3, 4)\n"
        'void func_80209CD8_de(void);\nchar *s = "func_80209CD8_de(1)";\n'
    )
    assert [(r[1], r[3]) for r in arity.rows_of(text, SYM)] == [("p", 0)]


def test_nested_commas_count_once():
    rows = arity.rows_of("void f(void) { func_80209CD8_de(h(1, 2), (3, 4), a[1]); }", SYM)
    assert rows[0][3] == 3


def test_body_digest_ignores_whitespace_not_code():
    a = arity.rows_of("void func_80209CD8_de(int a) { return; }", SYM)[0][5]
    b = arity.rows_of("void func_80209CD8_de(int a) {\n  return;\n}", SYM)[0][5]
    c = arity.rows_of("void func_80209CD8_de(int a) { a = 1; }", SYM)[0][5]
    assert a == b != c


ASM = """
glabel func_80209CD8_de
    /* 0 0 */  lui        $v0, %hi(D_1)
    /* 4 4 */  addiu      $sp, $sp, -0x18
    /* 8 8 */  beqz       $v0, .Lauto_1
    /* c c */   sw        $ra, 0x10($sp)
    /* 10 10 */  lw        $v0, 0x0($a0)
    /* 14 14 */  j         .Lauto_2
    /* 18 18 */   addiu    $a1, $zero, 0x384
  .Lauto_1:
    /* 1c 1c */  addiu     $a1, $zero, 0x1E
  .Lauto_2:
    /* 20 20 */  slt       $v0, $a1, $v0
    /* 24 24 */  jr        $ra
    /* 28 28 */   addiu    $sp, $sp, 0x18
"""


def test_asm_reads_only_what_is_not_written_first():
    got = arity.parse_asm(ASM)
    assert got["words"] == 1 and got["regs"] == ["a0"]


def test_asm_a_read_on_one_path_counts():
    text = ASM.replace("addiu     $a1, $zero, 0x1E", "nop")
    assert arity.parse_asm(text)["regs"] == ["a0", "a1"]


def test_asm_stack_and_float_arguments():
    text = """
    /* 0 0 */  addiu      $sp, $sp, -0x20
    /* 4 4 */  lw         $v0, 0x30($sp)
    /* 8 8 */  mov.s      $f0, $f12
    /* c c */  jr         $ra
    /* 10 10 */   nop
"""
    got = arity.parse_asm(text)
    assert got["stack"] == [4] and got["words"] == 5 and "f12" in got["regs"]


def test_asm_call_clobbers_argument_registers():
    text = """
    /* 0 0 */  jal        func_x
    /* 4 4 */   nop
    /* 8 8 */  addu       $v0, $a1, $v0
    /* c c */  jr         $ra
    /* 10 10 */   nop
"""
    assert arity.parse_asm(text)["words"] == 0


def census_of(files: dict[str, str], asm: dict | None = None):
    rows = {f: arity.rows_of(t, SYM) for f, t in files.items()}
    return arity.build(rows, asm or {}, {})


def test_verdicts():
    table = census_of({"a.c": DEF, "b.c": USER}, {"func_80209CD8_de": {"words": 1, "regs": ["a0"]}})
    entry = table["func_80209CD8_de"]
    assert entry["verdict"] == "disagree" and entry["score"] == 3
    assert {m["kind"] for m in entry["mismatches"]} == {"prototype", "call"}
    assert "asm reads 1 argument" in arity.explain("func_80209CD8_de", entry)
    agreed = census_of({"a.c": DEF, "b.c": "void g(void) { func_80209CD8_de(p); }"})
    assert agreed["func_80209CD8_de"]["verdict"] == "agreed"


def test_asm_reading_more_than_the_definition_has():
    table = census_of({"a.c": DEF}, {"func_80209CD8_de": {"words": 3, "regs": ["a0", "a1", "a2"]}})
    assert table["func_80209CD8_de"]["verdict"] == "asm"


def test_struct_by_value_does_not_claim_a_word_count():
    rows = {"a.c": arity.rows_of("void func_80209CD8_de(Vec3 v, int a) {}", SYM)}
    table = arity.build(rows, {"func_80209CD8_de": {"words": 4, "regs": []}}, {"Vec3": "struct"})
    assert table["func_80209CD8_de"]["verdict"] == "agreed"


def gate(tmp_path: Path, base: dict[str, str], new: dict[str, str]):
    root = tmp_path / "t"
    (root / "include").mkdir(parents=True)
    (root / "src").mkdir()
    for name, text in new.items():
        (root / "src" / name).write_text(text)
    return arity.check_change(
        root,
        None,
        ["src/" + k for k in sorted(set(base) | set(new))],
        lambda f: base.get(f.removeprefix("src/")),
        tmp_path / "state",
    )


BASE = {
    "a.c": DEF,
    "b.c": "extern void func_80209CD8_de(Brain *, Player *);\nvoid g(void) { func_80209CD8_de(brain, player); }\n",
}


def test_gate_refuses_an_added_mismatch(tmp_path):
    base = {"a.c": DEF, "b.c": "void g(void) { func_80209CD8_de(p); }\n"}
    new = {"a.c": DEF, "b.c": "void g(void) { func_80209CD8_de(p, q); }\n"}
    row = gate(tmp_path, base, new)["refused"][0]
    assert row["function"] == "func_80209CD8_de" and (row["base"], row["new"]) == (0, 1)
    assert row["file"] == "src/b.c" and "b.c:1 call with 2" in row["sites"][0]


def test_gate_accepts_a_fix(tmp_path):
    fixed = {"a.c": DEF, "b.c": "extern void func_80209CD8_de(Brain *);\nvoid g(void) { func_80209CD8_de(brain); }\n"}
    report = gate(tmp_path, BASE, fixed)
    assert report["refused"] == [] and report["checked"][0]["new"] == 0


def test_gate_refuses_editing_a_disagreeing_function_without_reducing(tmp_path):
    edited = {"a.c": DEF.replace("x = 1", "x = 2"), "b.c": BASE["b.c"]}
    refused = gate(tmp_path, BASE, edited)["refused"]
    assert refused and "without reducing" in refused[0]["why"]


def test_gate_leaves_an_unrelated_edit_alone(tmp_path):
    new = {"a.c": DEF, "b.c": BASE["b.c"] + "int unrelated;\n"}
    assert gate(tmp_path, BASE, new)["refused"] == []


def test_next_command_names_the_fix():
    assert arity.next_command("w.json", "f", ["call", "prototype"]).endswith("recipe work f --kind declaration")
    assert arity.next_command("w.json", "f", ["call"]).endswith("arity show f")


def test_frame_names_reads_the_stacks(tmp_path):
    path = tmp_path / "failure-stack.txt"
    path.write_text("#0 0x1 in func_80209CD8_de+0x1c\n#1 func_80208158_de ()\n")
    assert arity.frame_names([path]) == ["func_80209CD8_de", "func_80208158_de"]


def test_landing_gate_turns_a_refusal_into_a_blocking_finding(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import abu_decomp.behave.run as run_module
    from abu_decomp import land

    row = {
        "function": "func_80209CD8_de",
        "base": 0,
        "new": 1,
        "why": "adds 1 disagreement(s)",
        "detail": "func_80209CD8_de: defined with 1",
        "sites": ["src/b.c:1 call with 2"],
        "kinds": ["call"],
        "file": "src/b.c",
    }
    monkeypatch.setattr(arity, "check_change", lambda *a, **k: {"refused": [row], "checked": [], "counts": {}})
    monkeypatch.setattr(run_module, "configuration", lambda profile: {})
    monkeypatch.setattr(run_module, "work_dir", lambda config: tmp_path)
    landing = land.Landing.__new__(land.Landing)
    landing.config = {"arity": arity.settings(None)}
    landing.clock, landing.gates, landing.findings = (lambda: 0.0), [], []
    landing.cpu_mark = 0.0
    landing.tree, landing.extra = tmp_path, {"base": "B"}
    landing.profile = SimpleNamespace(source_file=Path("w.json"))
    landing.changed_files = lambda: ["src/b.c"]
    landing.git = lambda *a, **k: ""
    assert landing.arity() is False
    finding = landing.findings[0]
    assert finding["key"] == "land.arity" and finding["file"] == "src/b.c"
    assert finding["next"].endswith("arity show func_80209CD8_de") and "defined with 1" in finding["message"]
    assert landing.gates[0]["gate"] == "arity" and landing.gates[0]["pass"] is False


def test_notes_name_the_disagreeing_frame_first(monkeypatch):
    from types import SimpleNamespace

    table = census_of({"a.c": DEF, "b.c": USER}, {"func_80209CD8_de": {"words": 1, "regs": ["a0"]}})
    table.update(census_of({"c.c": "void func_80208158_de(int a) { }"}))
    fake = SimpleNamespace(table=lambda: table, close=lambda: None)
    monkeypatch.setattr(arity, "census_for", lambda profile, project=None: fake)
    found = arity.notes(
        SimpleNamespace(source_file="w.json"), ["func_80209CD8_de", "func_80208158_de", "func_80209CD8_de"]
    )
    assert [r["function"] for r in found] == ["func_80209CD8_de"]
    assert "first" in found[0]["first"] and found[0]["next"].endswith("recipe work func_80209CD8_de --kind declaration")


def test_notes_never_raise_without_a_project(monkeypatch):
    from types import SimpleNamespace

    from abu_decomp import DecompError

    def refuse(profile, project=None):
        raise DecompError("tools.config", "no project")

    monkeypatch.setattr(arity, "census_for", refuse)
    assert arity.notes(SimpleNamespace(source_file="w.json"), ["func_80209CD8_de"]) == []
