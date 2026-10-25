"""`decomp cleanup dead-decls`: dead declarations of a header folder, in layers that each leave a compiling tree."""

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
"""

OTHER = '#include "common.h"\nint main(void) { return gLive + MODE_B; }\nint K(void) { return USES_KIND(1); }\n'


def project(tmp_path: Path) -> Path:
    for name, text in {"include/COMMON/common.h": COMMON, "src/main.c": OTHER}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def names(found: dict, layer: int) -> set[str]:
    return set(found["layers"][layer]["names"])


def test_dead_declarations_come_in_layers_users_first(tmp_path: Path) -> None:
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    assert names(found, 0) >= {"gDeadUser", "User", "Helper"}  # nothing dead uses them
    assert "DeadUser" in names(found, 1) and "Dead" in names(found, 2) and "Vec" in names(found, 3)


def test_a_multi_name_extern_is_live_when_any_name_is_used(tmp_path: Path) -> None:
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    every = set().union(*(names(found, k) for k in range(len(found["layers"]))))
    assert "gLive" not in every and "gUnused" not in every and "gAlso" not in every


def test_an_enum_constant_used_elsewhere_keeps_its_enum_and_a_macro_keeps_what_it_spells(tmp_path: Path) -> None:
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    every = set().union(*(names(found, k) for k in range(len(found["layers"]))))
    assert not every & {"Mode", "MODE_A", "MODE_B", "Kind", "KIND_X", "KIND_Y"}


def test_a_name_used_only_by_a_live_inline_stays_live(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/use.c").write_text("int a = User();\n")
    found = deaddecls.run(project(tmp_path), "include/COMMON")
    every = set().union(*(names(found, k) for k in range(len(found["layers"]))))
    assert "Only_In_Inline" not in every and "User" not in every


def test_packs_apply_in_order_and_layer_zero_is_written_with_apply(tmp_path: Path) -> None:
    root = project(tmp_path)
    packs = tmp_path / "packs"
    found = deaddecls.run(root, "include/COMMON", packs=packs, apply=True)
    assert [p.name for p in sorted(packs.iterdir())] == [f"dead-decls-L{k}.diff" for k in range(len(found["layers"]))]
    text = (root / "include/COMMON/common.h").read_text()
    assert "gDeadUser" not in text and "typedef struct DeadUser" in text  # layer 0 only
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "--", "."], check=True)
    for pack in sorted(packs.iterdir()):
        subprocess.run(["git", "-C", str(root), "apply", str(pack)], check=True)
    final = (root / "include/COMMON/common.h").read_text()
    assert "Dead" not in final and "Vec" not in final and "the vector" not in final  # its comment goes with it
    assert "/* live externs */" in final and "enum Mode" in final
