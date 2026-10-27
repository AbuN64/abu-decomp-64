"""`decomp cleanup owners` without `--dir`: unit-local externs hoisted into the owning header, as batches."""

import json
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


@pytest.mark.parametrize("cap,batches", [(300.0, 1), (0.5, 2)])
def test_batches_are_split_to_stay_under_the_estimated_cpu_cap(tmp_path: Path, cap: float, batches: int) -> None:
    _, found = moved(tmp_path, None, batch_cpu=cap, unit_cpu=0.25)
    assert len(found["batches"]) == batches
    manifest = json.loads((tmp_path / "out" / "externs.json").read_text())
    shared = [b for b in manifest["batches"] if len(b["destinations"]) > 1]
    assert all(b["estimated_cpu"] <= cap for b in shared)  # a header alone may exceed it, and says so
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
