"""`decomp cleanup owners` without `--dir`: unit-local externs hoisted into the owning header, as batches."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from abu_decomp import externs, owners

MAP = (
    "unit\tmodule\tfolder\tprefix\theader\tstart\tend\n"
    "src/a/a.c\tmod/A\tA\tA\tinclude/A/A.H\t-\t-\n"
    "src/b/b.c\tmod/B\tB\tB\tinclude/B/B.H\t-\t-\n"
    "src/c/c.c\tmod/C\tC\tC\tinclude/C/C.H\t-\t-\n"
    "src/rom/r.c\tmod/R\tR\tR\tinclude/R/R.H\t80001000\t80002000\n"
)
GUARD = "#ifndef {0}\n#define {0}\n#endif\n"
FILES = {
    "docs/MODULES.tsv": MAP,
    "include/A/A.H": GUARD.format("A_H"),
    "include/B/B.H": GUARD.format("B_H"),
    "include/C/C.H": GUARD.format("C_H"),
    "include/R/R.H": GUARD.format("R_H"),
    # definer ownership: gScore is defined in a.c, used by b.c and c.c
    "src/a/a.c": '#include "A/A.H"\nint gScore = 1;\nint Helper(int x) { return x; }\n',
    "src/b/b.c": '#include "B/B.H"\nextern int gScore;\nextern int Helper(int x);\nint useb(void) { return gScore; }\n',
    "src/c/c.c": '#include "C/C.H"\nextern int gScore;\nextern int Helper(int x);\nextern int D_80001500;\n'
    "extern int gMajor;\nint usec(void) { return gScore + D_80001500; }\n",
    "src/rom/r.c": '#include "R/R.H"\nint fr(void) { return 0; }\n',
}


# the map spells a header from its module the same way twice: `mod/X/Q` -> `include/X/Q.H` (the rule the planner learns)
MAP2 = MAP + "src/x/q.c\tmod/X/Q\tX\tX\tinclude/X/Q.H\t-\t-\nsrc/x/p.c\tmod/X/P\tX\tX\tinclude/X/P.H\t-\t-\n"


def project(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    for name, text in {**FILES, **(extra or {})}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def moved(tmp_path: Path, extra: dict[str, str] | None = None, **kw: float) -> tuple[Path, dict]:
    root = project(tmp_path, extra)
    out = tmp_path / "out"
    return root, externs.plan(root, None, out, **kw)


def table(out: Path) -> dict[str, list[str]]:
    rows = [r.split("\t") for r in (out / "externs.tsv").read_text().splitlines()[1:]]
    return {r[0]: r for r in rows}


def test_a_defined_symbol_goes_to_the_header_of_its_definer(tmp_path: Path) -> None:
    _, found = moved(tmp_path)
    row = table(tmp_path / "out")["gScore"]
    assert row[1:4] == ["mod/A", "include/A/A.H", "definer"]
    assert found["unit_extern_lines_before"] == 6 and found["unit_extern_ids_before"] == 4


def test_a_symbol_defined_outside_c_belongs_to_the_unit_owning_its_address_range(tmp_path: Path) -> None:
    moved(tmp_path)
    assert table(tmp_path / "out")["D_80001500"][1:4] == ["mod/R", "include/R/R.H", "address range"]


def test_a_symbol_nobody_owns_goes_to_its_majority_user(tmp_path: Path) -> None:
    extra = {"src/b/b.c": FILES["src/b/b.c"] + "extern int gMajor;\n", "src/a/a.c": FILES["src/a/a.c"]}
    extra["src/c/c.c"] = FILES["src/c/c.c"] + "extern int gMajor;\n"  # c declares it twice, b once
    moved(tmp_path, extra)
    assert table(tmp_path / "out")["gMajor"][1:4] == ["mod/C", "include/C/C.H", "majority user"]


def test_declarations_that_conflict_are_refused_with_their_prototypes(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gMajor;\n",
        "src/c/c.c": FILES["src/c/c.c"].replace("extern int gMajor;", "extern long gMajor;"),
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "gMajor")
    assert refusal["reason"] == "declarations conflict" and refusal["protos"] == ["int gMajor", "long gMajor"]
    assert "gMajor" not in table(tmp_path / "out")


def test_an_includer_of_the_destination_that_spells_the_name_otherwise_is_refused(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": FILES["src/b/b.c"].replace('#include "B/B.H"', '#include "B/B.H"\n#include "A/A.H"')
        + "static int gScore2;\n",
        "src/c/c.c": FILES["src/c/c.c"].replace("int usec", "#define Helper(x) (x)\nint usec"),
        "src/rom/r.c": '#include "R/R.H"\n#include "A/A.H"\ntypedef int gScore;\nint fr(void) { return 0; }\n',
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "gScore")
    assert refusal["reason"].startswith("name clashes in src/rom/r.c")
    assert "gScore" not in table(tmp_path / "out")


def test_the_batch_deletes_every_redeclaration_and_the_header_holds_one_extern(tmp_path: Path) -> None:
    root, found = moved(tmp_path)
    out = tmp_path / "out"
    assert found["batches"] and (out / found["batches"][0]["batch"]).is_file()
    for batch in found["batches"]:
        subprocess.run(["git", "-C", str(root), "apply", "--check", str(out / batch["batch"])], check=True)
        subprocess.run(["git", "-C", str(root), "apply", str(out / batch["batch"])], check=True)
    header = (root / "include/A/A.H").read_text()
    assert header.count("extern int gScore;") == 1 and header.count("extern int Helper(int x);") == 1
    for unit in ("src/b/b.c", "src/c/c.c"):
        text = (root / unit).read_text()
        assert "extern int gScore;" not in text and "extern int Helper" not in text
        assert '#include "A/A.H"' in text  # a unit that no longer saw the destination includes it
    assert header.rstrip().endswith("#endif")
    after = externs.plan(root, None, None)
    assert after["unit_extern_lines_before"] == found["unit_extern_lines_after"]
    assert found["unit_extern_lines_after"] == 0 or found["refused_ids"] > 0


ISLAND = {  # a second module pair that shares no file with the first: its ids can ride a batch of their own
    "include/P/P.H": GUARD.format("P_H"),
    "include/Q/Q.H": GUARD.format("Q_H"),
    "docs/MODULES.tsv": MAP
    + "src/p/p.c\tmod/P\tP\tP\tinclude/P/P.H\t-\t-\nsrc/q/q.c\tmod/Q\tQ\tQ\tinclude/Q/Q.H\t-\t-\n",
    "src/p/p.c": '#include "P/P.H"\nint gIsle = 1;\n',
    "src/q/q.c": '#include "Q/Q.H"\nextern int gIsle;\nint useq(void) { return gIsle; }\n',
}


@pytest.mark.parametrize("cap,batches", [(300.0, 1), (0.5, 2)])
def test_batches_are_split_to_stay_under_the_estimated_cpu_cap(tmp_path: Path, cap: float, batches: int) -> None:
    _, found = moved(tmp_path, ISLAND, batch_cpu=cap, unit_cpu=0.25)
    assert len(found["batches"]) == batches
    manifest = json.loads((tmp_path / "out" / "externs.json").read_text())
    island = [b for b in manifest["batches"] if b["destinations"] == ["include/P/P.H"]]
    assert bool(island) == (batches == 2) and all(
        b["estimated_cpu"] <= cap for b in island
    )  # a group alone may exceed it
    assert sum(b["ids"] for b in manifest["batches"]) == found["planned_ids"]


def test_the_header_folder_mode_ignores_unit_externs(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "include/COMMON").mkdir()
    (root / "include/COMMON/common.h").write_text("extern int gScore;\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    found = owners.run(root, "include/COMMON")
    assert found["declarations"] == 1 and next(r for r in found["rows"])["owner"] == "mod/A"


def test_a_crlf_tree_keeps_its_line_endings_and_the_diff_names_each_file(tmp_path: Path) -> None:
    extra = {n: t.replace("\n", "\r\n") for n, t in FILES.items() if n != "docs/MODULES.tsv"}
    root, found = moved(tmp_path, extra)
    batch = tmp_path / "out" / found["batches"][0]["batch"]
    diff = batch.read_bytes()
    assert diff.startswith(b"# MSG: Hoist ") and diff.index(b"\n") <= 79
    assert b"diff --git a/include/A/A.H b/include/A/A.H\n" in diff
    subprocess.run(["git", "-C", str(root), "apply", "--check", str(batch)], check=True)
    assert b"\n+extern int gScore;\r\n" in diff and b'+#include "A/A.H"\r\n' in diff


def test_a_prototype_spelling_a_type_the_destination_cannot_see_is_refused(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": '#include "B/B.H"\ntypedef struct Vec { int x; } Vec;\nextern Vec gPos;\n'
        "int useb(void) { return gPos.x; }\n",
        "src/a/a.c": '#include "A/A.H"\ntypedef struct Vec { int x; } Vec;\nVec gPos;\nint gScore = 1;\n',
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "gPos")
    assert refusal["reason"].startswith("type not visible from include/A/A.H")
    assert "gPos" not in table(tmp_path / "out")


def test_a_unit_redeclaring_a_name_its_header_declares_is_cleaned_whatever_its_text(tmp_path: Path) -> None:
    extra = {
        "include/C/C.H": GUARD.format("C_H").replace("#endif", "extern s32 gMajor;\n#endif"),
        "src/c/c.c": FILES["src/c/c.c"].replace("extern int gMajor;", "extern signed int gMajor;"),
        "src/b/b.c": FILES["src/b/b.c"].replace('#include "B/B.H"', '#include "B/B.H"\n#include "C/C.H"')
        + "extern int gMajor;\n",
    }
    _, found = moved(tmp_path, extra)
    row = table(tmp_path / "out")["gMajor"]
    assert row[2] == "include/C/C.H" and "declared in header" in row[3]
    diff = (tmp_path / "out" / found["batches"][0]["batch"]).read_text()
    assert "-extern signed int gMajor;" in diff and "-extern int gMajor;" in diff
    assert "+extern s32 gMajor;" not in diff


def test_a_blind_unit_whose_text_differs_from_the_header_declaration_is_refused(tmp_path: Path) -> None:
    extra = {
        "include/C/C.H": GUARD.format("C_H").replace("#endif", "extern s32 gMajor;\n#endif"),
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gMajor;\n",
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "gMajor")
    assert refusal["reason"] == "declarations conflict" and refusal["protos"] == ["int gMajor", "s32 gMajor"]


def test_a_declaration_with_another_parameter_count_than_the_definition_is_refused(tmp_path: Path) -> None:
    extra = {"src/b/b.c": FILES["src/b/b.c"].replace("extern int Helper(int x);", "extern int Helper(int, int);")}
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "Helper")
    assert refusal["reason"] == "declaration parameter count differs from the definition"
    assert "int Helper(int, int)" in refusal["protos"] and "int Helper(int x)" in refusal["protos"]
    assert "Helper" not in table(tmp_path / "out")


@pytest.mark.parametrize(
    "proto,count",
    [("int f(void)", 0), ("int f()", None), ("int f(int, char *)", 2), ("void (*f)(int, int)", 2), ("int g", None)],
)
def test_the_parameter_count_of_a_prototype(proto: str, count: int | None) -> None:
    assert externs.arity(proto) == count


def test_a_new_include_goes_before_the_first_use_not_after_a_later_conditional_include(tmp_path: Path) -> None:
    body = '#include "B/B.H"\nextern int gScore;\nint useb(void) { return gScore; }\n#if 1\n#include "Z.H"\n#endif\n'
    root, _ = moved(tmp_path, {"src/b/b.c": body, "include/Z.H": GUARD.format("Z_H")})
    subprocess.run(["git", "-C", str(root), "apply", str(tmp_path / "out" / "externs-batch-01.diff")], check=True)
    lines = (root / "src/b/b.c").read_text().splitlines()
    assert lines.index('#include "A/A.H"') < lines.index("int useb(void) { return gScore; }")


def test_a_unit_gaining_the_destination_header_is_refused_when_it_spells_a_name_the_closure_declares(
    tmp_path: Path,
) -> None:
    extra = {
        "include/A/A.H": "#ifndef A_H\n#define A_H\nextern int gOther;\n#endif\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern long gOther;\n",
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "gScore")
    assert refusal["reason"].startswith("include clashes in include/A/A.H:gOther")


def test_a_batch_section_counts_blank_lines_as_git_does(tmp_path: Path) -> None:
    old = ["a\n", "extern int x;\n", "\n", "extern int y;\n", "\n", "b\n"]
    new = ["a\n", "\n", "\n", "b\n"]
    section = externs.git_diff("src/u.c", old, new)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/u.c").write_text("".join(old))
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    counts = subprocess.run(
        ["git", "-C", str(repo), "apply", "--numstat", "-"], input=section.encode(), capture_output=True, check=True
    ).stdout.decode()
    assert counts.split() == ["0", "2", "src/u.c"]


def test_an_include_under_a_condition_does_not_make_the_header_visible_to_every_build(tmp_path: Path) -> None:
    body = (
        '#include "B/B.H"\n#if VERSION_EU\n#include "A/A.H"\n#endif\nextern int gScore;\nint useb(void) { return 1; }\n'
    )
    root, _ = moved(tmp_path, {"src/b/b.c": body})
    subprocess.run(["git", "-C", str(root), "apply", str(tmp_path / "out" / "externs-batch-01.diff")], check=True)
    lines = (root / "src/b/b.c").read_text().splitlines()
    assert lines.count('#include "A/A.H"') == 2 and lines[1] == '#include "A/A.H"'  # the plain include goes first


def test_unconditional_includes_skip_conditions_but_not_the_include_guard() -> None:
    text = '#ifndef X_H\n#define X_H\n#include "a.h"\n#if 1\n#include "b.h"\n#endif\n#include "c.h"\n#endif\n'
    assert externs.unconditional_includes(text, True) == ["a.h", "c.h"]
    assert externs.unconditional_includes(text, False) == []


def land(root: Path, out: Path, found: dict) -> None:
    for batch in found["batches"]:
        subprocess.run(["git", "-C", str(root), "apply", "--check", str(out / batch["batch"])], check=True)
        subprocess.run(["git", "-C", str(root), "apply", str(out / batch["batch"])], check=True)


def test_c_multi_declarator_lines_are_split_and_then_planned(tmp_path: Path) -> None:
    extra = {
        "src/a/a.c": FILES["src/a/a.c"] + "int gA;\nint *gB;\nint gC[3];\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gA, *gB, gC[3];\nint useb2(void) { return gA; }\n",
    }
    root, found = moved(tmp_path, {**extra, "src/c/c.c": FILES["src/c/c.c"].replace("extern int gScore;\n", "")})
    out = tmp_path / "out"
    assert found["split_units"] == 1
    assert all({"estimated_cpu", "recompiled_units", "lines_removed"} <= set(b) for b in found["batches"])
    rows = table(out)
    assert {i: rows[i][2] for i in ("gA", "gB", "gC")} == dict.fromkeys(("gA", "gB", "gC"), "include/A/A.H")
    batch = (out / "externs-batch-01.diff").read_text()  # the unit's split and its move are one diff from main
    assert "-extern int gA, *gB, gC[3];" in batch and "+extern int gA," not in batch
    assert not (out / "externs-batch-00-split.diff").exists()
    land(root, out, found)
    assert "gA," not in (root / "src/b/b.c").read_text()
    header = (root / "include/A/A.H").read_text()
    assert all(f in header for f in ("extern int gA;", "extern int*gB;", "extern int gC[3];"))


def test_c_a_declarator_the_split_cannot_state_stays_refused(tmp_path: Path) -> None:
    extra = {"src/b/b.c": FILES["src/b/b.c"] + "extern int gP, (*gFn)(int);\n"}
    _, found = moved(tmp_path, extra)
    assert found["split_units"] == 0
    assert externs.split_declaration("extern int gP, (*gFn)(int);") is None
    assert externs.split_declaration("extern int x = 1, y;") is None
    assert externs.split_declaration("extern void f(), g(unsigned int, char*), *h(void);") == [
        "extern void f();",
        "extern void g(unsigned int, char*);",
        "extern void *h(void);",
    ]
    assert externs.split_declaration("extern int a, (*fp)(int);") is None
    assert externs.split_declaration("extern char *a, b[2];") == ["extern char *a;", "extern char b[2];"]


def test_a_module_file_that_is_itself_a_unit_belongs_to_its_module(tmp_path: Path) -> None:
    extra = {
        "docs/MODULES.tsv": MAP2
        + "src/m/m1.c\tsrc/m/m.c\tM\tM\tinclude/m/m.H\t-\t-\nsrc/n/n1.c\tsrc/n/n.c\tN\tN\t-\t-\t-\n",
        "src/n/n.c": "int gMerged = 3;\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gMerged;\n",
    }
    moved(tmp_path, extra)
    assert table(tmp_path / "out")["gMerged"][1:3] == ["src/n/n.c", "include/n/n.H"]


def test_a_keyword_a_source_file_macros_is_not_a_type_the_header_must_see(tmp_path: Path) -> None:
    extra = {
        "docs/MODULES.tsv": MAP2 + "src/n/n.c\tmod/N\tN\tN\t-\t-\t-\n",
        "src/n/n.c": "unsigned char gBytes[4] = {1};\n",
        "src/shim/shim.c": "#define char bogus_type\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern unsigned char gBytes[4];\n",
    }
    _, found = moved(tmp_path, extra)
    assert table(tmp_path / "out")["gBytes"][2] == "include/N.H" and found["created_headers"] == ["include/N.H"]


def test_a_the_planner_creates_the_missing_module_header_and_includes_it_in_definer_and_users(tmp_path: Path) -> None:
    extra = {
        "docs/MODULES.tsv": MAP2 + "src/n/n.c\tmod/N\tN\tN\t-\t-\t-\n",
        "src/n/n.c": "int gNew = 3;\nint gOther[4] = {1};\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gNew;\nextern int gOther[4];\n",
    }
    root, found = moved(tmp_path, extra)
    out = tmp_path / "out"
    assert found["created_headers"] == ["include/N.H"]
    assert table(out)["gNew"][2] == "include/N.H"
    diff = (out / found["batches"][0]["batch"]).read_text()
    assert "new file mode 100644" in diff and "--- /dev/null" in diff
    land(root, out, found)
    header = (root / "include/N.H").read_text()
    assert header.startswith("#ifndef N_H\n#define N_H\n") and "extern int gNew;" in header
    assert "extern int gOther[4];" in header and header.rstrip().endswith("#endif")
    for unit in ("src/n/n.c", "src/b/b.c"):  # the definer and the user both include it
        assert '#include "N.H"' in (root / unit).read_text()
    assert "extern int gNew" not in (root / "src/b/b.c").read_text()


def test_a_the_created_header_includes_the_provider_of_the_types_it_spells(tmp_path: Path) -> None:
    extra = {
        "docs/MODULES.tsv": MAP2 + "src/n/n.c\tmod/N\tN\tN\t-\t-\t-\n",
        "include/T.H": "#ifndef T_H\n#define T_H\ntypedef struct Thing { int v; } Thing;\n#endif\n",
        "src/n/n.c": '#include "T.H"\nThing gThing;\n',
        "src/b/b.c": FILES["src/b/b.c"] + "extern Thing gThing;\n",
    }
    root, found = moved(tmp_path, extra)
    assert table(tmp_path / "out")["gThing"][2] == "include/N.H"
    land(root, tmp_path / "out", found)
    assert '#include "T.H"' in (root / "include/N.H").read_text()


def test_b_one_type_under_two_spellings_is_one_declaration(tmp_path: Path) -> None:
    extra = {
        "include/R/R.H": "#ifndef R_H\n#define R_H\ntypedef float f32;\n#endif\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern float D_80001600;\nextern int func_80001700(int a);\n",
        "src/c/c.c": FILES["src/c/c.c"] + "extern f32 D_80001600;\nextern int func_80001700(int b);\n",
    }
    _, found = moved(tmp_path, extra)
    rows = table(tmp_path / "out")
    assert rows["D_80001600"][2] == "include/R/R.H" and rows["D_80001600"][4] in ("float D_80001600", "f32 D_80001600")
    assert "func_80001700" in rows
    assert not [r for r in found["refused"] if r["id"] in ("D_80001600", "func_80001700")]


def test_b_what_the_definition_cannot_settle_is_residue_with_both_prototypes(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": FILES["src/b/b.c"] + "extern float D_80001600;\n",
        "src/c/c.c": FILES["src/c/c.c"] + "extern int D_80001600;\n",
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "D_80001600")
    assert refusal["protos"] == ["float D_80001600", "int D_80001600"]


def test_a_split_unit_no_move_edits_gets_the_split_batch_alone(tmp_path: Path) -> None:
    extra = {"src/z/z.c": "extern int gZ1, gZ2;\n"}  # in no module of the map: refused, still split
    _, found = moved(tmp_path, extra)
    assert {r["id"] for r in found["refused"]} >= {"gZ1", "gZ2"}
    assert found["batches"][0]["batch"] == "externs-batch-00-split.diff" and found["split_units"] == 1
    split = (tmp_path / "out" / "externs-batch-00-split.diff").read_text()
    assert "+extern int gZ1;" in split and "+extern int gZ2;" in split


def test_batches_share_no_file_and_each_applies_to_main_alone(tmp_path: Path) -> None:
    root, found = moved(tmp_path, ISLAND, batch_cpu=0.5, unit_cpu=0.25)
    out = tmp_path / "out"
    names = [b["batch"] for b in found["batches"]]
    assert len(names) == 2
    files = [set(re.findall(r"^\+\+\+ b/(\S+)", (out / n).read_text(), re.M)) for n in names]
    assert not files[0] & files[1]
    for n in names:  # each one checks against main with the others unapplied
        subprocess.run(["git", "-C", str(root), "apply", "--check", str(out / n)], check=True)


def test_b_a_clash_with_a_name_the_plan_itself_removes_from_the_unit_is_no_clash(tmp_path: Path) -> None:
    extra = {
        "include/A/A.H": "#ifndef A_H\n#define A_H\nextern int gOther;\n#endif\n",
        "src/a/a.c": FILES["src/a/a.c"] + "int gOther = 2;\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gOther;\n",
    }
    _, found = moved(tmp_path, extra)
    assert "gScore" in {m for m in table(tmp_path / "out")}
    assert not [r for r in found["refused"] if r["id"] in {"gScore", "gOther"}]


def test_c_split_only_batch_has_a_commit_subject_within_the_cap(tmp_path: Path) -> None:
    moved(tmp_path, {"src/d/d.c": "extern int gQ, gR;\n"})  # no definer: refused, only split
    diff = (tmp_path / "out" / "externs-batch-00-split.diff").read_bytes()
    assert diff.startswith(b"# MSG: Split ") and diff.index(b"\n") <= 79


def test_c_a_split_the_stamp_gate_refuses_is_undone_and_the_line_stays_refused(tmp_path: Path) -> None:
    extra = {
        "src/a/a.c": FILES["src/a/a.c"] + "int gA;\nint gB;\n",
        "src/b/b.c": FILES["src/b/b.c"] + "extern int gA, gB;\nint useb2(void) { return gA; }\n",
    }
    root = project(tmp_path, extra)

    def gate(text: str) -> list[str]:  # a data gate that cannot prove gB, so no diff may rewrite a line naming it
        return ["INTERIOR gB: unproved" for line in text.splitlines() if line.startswith("-") and "gB" in line]

    found = externs.plan(root, None, tmp_path / "out", gate=gate)
    batches = "".join(p.read_text() for p in (tmp_path / "out").glob("externs-batch-*.diff"))
    assert "-extern int gA, gB;" not in batches
    assert found["split_units"] == 0
    reasons = {r["id"]: r["reason"] for r in found["refused"]}
    assert reasons["gA"] == reasons["gB"] == "declared together with other names"
    assert found["unit_extern_ids_before"] - found["unit_extern_ids_after"] == found["planned_ids"]


def test_b_an_anonymous_aggregate_definition_is_residue_never_an_extern_struct(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": FILES["src/b/b.c"] + "extern unsigned char D_80001700[];\n",
        "src/c/c.c": FILES["src/c/c.c"] + "struct {\n    int w;\n} D_80001700 = {1};\n",
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "D_80001700")
    assert refusal["reason"] == "definition names no type to declare"
    assert refusal["protos"] == ["struct", "unsigned char D_80001700[]"]
    assert not any("extern struct;" in f.read_text() for f in (tmp_path / "out").glob("*.diff"))


def test_b_a_declaration_of_another_type_than_the_definition_is_residue_with_both_prototypes(tmp_path: Path) -> None:
    extra = {
        "src/b/b.c": FILES["src/b/b.c"] + "extern char D_80001800[];\n",
        "src/c/c.c": FILES["src/c/c.c"] + "char D_80001800;\n",
    }
    _, found = moved(tmp_path, extra)
    refusal = next(r for r in found["refused"] if r["id"] == "D_80001800")
    assert refusal["reason"] == "declaration differs from the definition"
    assert refusal["protos"] == ["char D_80001800", "char D_80001800[]"]
