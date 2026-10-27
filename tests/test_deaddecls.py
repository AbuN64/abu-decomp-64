"""`decomp cleanup dead-decls`: the dead declarations of a folder to a fixpoint, as one batch per module folder."""

import subprocess
from pathlib import Path

from abu_decomp import deaddecls

COMMON = """/* the vector */
typedef struct Vec { float x; } Vec;
typedef struct Dead { Vec v; } Dead;
typedef struct DeadUser { Dead d; } DeadUser;
extern DeadUser gDeadUser;
/* live externs */
extern int gLive, gAlso, gUnused;
enum Mode { MODE_A, MODE_B };
typedef enum { KIND_X, KIND_Y } Kind;
static inline int Helper(Vec *v) { return (int)v->x + sizeof(Dead); }
void Only_In_Inline(void);
static inline int User(void) { Only_In_Inline(); return 0; }
#define USES_KIND(k) ((k) == KIND_Y)
typedef struct Meter { int m; } Meter;
typedef struct Sized { Meter m; } Sized;
typedef char Sized_size_check[(sizeof(Sized) == 8) ? 1 : -1];
typedef int Table[4];
typedef struct Dropped { int a; } Dropped;
"""

OTHER = '#include "common.h"\nint main(void) { return gLive + MODE_B; }\nint K(void) { return USES_KIND(1); }\n'


def project(tmp_path: Path) -> Path:
    for name, text in {"include/COMMON/common.h": COMMON, "src/main.c": OTHER}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def names(found: dict, scope: str = "header") -> set[str]:
    return {n for row in found["layers"] if row["scope"] == scope for n in row["names"]}


def test_dead_declarations_are_removed_users_first_in_layers(tmp_path: Path) -> None:
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    sizes = [row["layer"] for row in found["layers"]]
    assert sizes == [0, 1, 2, 3]  # 4 layers
    assert names(found) >= {"gDeadUser", "User", "Helper", "DeadUser", "Dead", "Vec"}


def test_a_multi_name_extern_is_live_when_any_name_is_used(tmp_path: Path) -> None:
    every = names(deaddecls.run(project(tmp_path), "include/COMMON"))
    assert "gLive" not in every and "gUnused" not in every and "gAlso" not in every


def test_an_enum_constant_used_elsewhere_keeps_its_enum_and_a_macro_keeps_what_it_spells(tmp_path: Path) -> None:
    every = names(deaddecls.run(project(tmp_path), "include/COMMON"))
    assert not every & {"Mode", "MODE_A", "MODE_B", "Kind", "KIND_X", "KIND_Y"}


def test_a_name_used_only_by_a_live_inline_stays_live(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/use.c").write_text("int a = User();\n")
    every = names(deaddecls.run(project(tmp_path), "include/COMMON"))
    assert "Only_In_Inline" not in every and "User" not in every


def test_all_layers_are_one_batch_and_apply_writes_the_fixpoint(tmp_path: Path) -> None:
    root = project(tmp_path)
    packs = tmp_path / "packs"
    found = deaddecls.run(root, "include/COMMON", packs=packs, apply=True)
    assert [b["batch"] for b in found["batches"]] == ["dead-decls-00-include_COMMON.diff"]
    assert sorted(p.name for p in packs.iterdir()) == ["dead-decls-00-include_COMMON.diff", "dead-decls.json"]
    final = (root / "include/COMMON/common.h").read_text()
    assert "Dead" not in final and "Vec" not in final and "the vector" not in final  # its comment goes with it
    assert "/* live externs */" in final and "enum Mode" in final
    assert deaddecls.run(root, "include/COMMON")["dead"] == 0  # a re-run finds nothing
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "--", "."], check=True)
    subprocess.run(["git", "-C", str(root), "apply", str(packs / "dead-decls-00-include_COMMON.diff")], check=True)
    assert (root / "include/COMMON/common.h").read_text() == final


def test_a_preview_leaves_the_tree_alone(tmp_path: Path) -> None:
    root = project(tmp_path)
    before = (root / "include/COMMON/common.h").read_text()
    found = deaddecls.run(root, "include")
    assert found["dead"] > 0 and (root / "include/COMMON/common.h").read_text() == before


UNIT = """#include "unit.h"
typedef struct Local { int a; } Local;
typedef struct LocalUser { Local l; } LocalUser;
extern int gGone;
extern int gKept;
void Gone(int x);
void Kept(void);
struct Tag { int t; };
int (*gFn)(int);
int gData[2] = { 1, 2 };
int Func(void) { return gKept + UNIT_MACRO; }
void Entry(void) { Kept(); }
"""


def units(tmp_path: Path) -> Path:
    for name, text in {
        "src/game/unit.c": UNIT,
        "src/game/unit.h": "#define UNIT_MACRO Local_Macro\n",
        "src/game/owner.c": "#include \"part.c\"\nextern int gPart;\nint x;\n",
        "src/game/part.c": "extern int gInPart;\nint y = 1;\n",
        "include/COMMON/common.h": "extern int gGone;\nextern int gKept;\ntypedef struct Local { int a; } Local;\n",
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def test_a_unit_drops_the_declarations_it_never_spells_again_and_keeps_every_definition(tmp_path: Path) -> None:
    root = units(tmp_path)
    found = deaddecls.run(root, "src", apply=True)
    assert names(found, "source") == {"Local", "LocalUser", "gGone", "Gone", "Tag", "gPart"}
    text = (root / "src/game/unit.c").read_text()
    for kept in ("extern int gKept;", "void Kept(void);", "int (*gFn)(int);", "int gData[2]", "int Func(void)", "Entry"):
        assert kept in text
    for gone in ("Local", "gGone", "Gone(int", "struct Tag"):
        assert gone not in text.replace("Local_Macro", "")
    assert "gInPart" in (root / "src/game/part.c").read_text()  # an included source is part of its includer


def test_a_source_scope_run_leaves_headers_alone_and_a_header_freed_by_it_goes_in_a_later_batch(tmp_path: Path) -> None:
    root = units(tmp_path)
    found = deaddecls.run(root, ".", packs=tmp_path / "packs")
    kinds = [b["scope"] for b in found["batches"]]
    assert kinds == sorted(kinds, key=("source", "header").index) and "header" in kinds
    assert [b["folders"] for b in found["batches"] if b["scope"] == "source"] == [["src/game"]]
    assert "gGone" in names(found, "header") and "Local" in names(found, "header")  # no unit spells them any more


def test_names_in_preprocessor_lines_inside_a_chunk_are_live_in_every_branch(tmp_path: Path) -> None:
    text = (
        "typedef struct OnlyMacro { int a; } OnlyMacro;\n"
        "extern int gOnlyIf;\nextern int gDead;\n"
        "DECLARE_THING(x)\n"  # no semicolon: the next lines join this chunk
        "#if EU\n#define PICK(v) \\\n  ((v) + (int)sizeof(OnlyMacro))\n#elif defined(gOnlyIf)\n#endif\n"
        "extern int gTail;\n"
    )
    (tmp_path / "include/COMMON").mkdir(parents=True)
    (tmp_path / "include/COMMON/m.h").write_text(text)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.c").write_text("int main(void) { return 0; }\n")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    found = deaddecls.run(tmp_path, "include/COMMON")
    dead = {n for layer in found["layers"] for n in layer["names"]}
    assert "OnlyMacro" not in dead and "gOnlyIf" not in dead
    assert "gDead" in dead


def test_a_size_assertion_is_a_root_with_the_types_it_measures(tmp_path: Path) -> None:
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    every = names(found)
    assert not every & {"Sized", "Sized_size_check", "Meter"}  # the assertion, its record and that record's types stay
    assert {"Table", "Dropped"} <= every  # an ordinary array typedef and an unused record are still dead
