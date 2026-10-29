"""`decomp cleanup owners`: the owning module of every declaration of a header folder."""

import subprocess
from pathlib import Path

from abu_decomp import owners

COMMON = """#ifndef COMMON_H
#define COMMON_H
typedef struct Pad { int x; } Pad;
typedef struct Menu { int m; } Menu;
typedef struct Wide { int w; } Wide;
extern int gScore;
void Menu_Draw(Pad *p);
#define MENU_MAX 4
extern int gNobody;
#endif
"""
MAP = (
    "unit\tmodule\tfolder\tprefix\theader\n"
    "src/menu/a.c\tsrc/game/MENU/A.CPP\tgame/MENU\tMenu\tinclude/MENU/A.H\n"
    "src/menu/b.c\tsrc/game/MENU/A.CPP\tgame/MENU\tMenu\tinclude/MENU/A.H\n"
    "src/play/p.c\tsrc/game/PLAY/P.CPP\tgame/PLAY\tPlay\tinclude/PLAY/P.H\n"
    "src/x/1.c\tsrc/game/X/1.CPP\tgame/X\tX\tinclude/X/1.H\n"
    "src/x/2.c\tsrc/game/X/2.CPP\tgame/X\tX\tinclude/X/2.H\n"
    "src/x/3.c\tsrc/game/X/3.CPP\tgame/X\tX\tinclude/X/3.H\n"
    "src/x/4.c\tsrc/game/X/4.CPP\tgame/X\tX\tinclude/X/4.H\n"
)
UNITS = {
    "src/menu/a.c": (
        "int gScore = 1;\nvoid Menu_Draw(Pad *p) { Menu m; (void)m; (void)p; }\nint f(void) { return MENU_MAX; }\n"
    ),
    "src/menu/b.c": "int g(void) { Menu m; return sizeof(Menu) + MENU_MAX + gScore; }\n",
    "src/play/p.c": "int h(Pad *p) { return p->x + gScore; }\n",
    "src/x/1.c": "int a(Wide *w) { return w->w; }\n",
    "src/x/2.c": "int b(Wide *w) { return w->w; }\n",
    "src/x/3.c": "int c(Wide *w) { return w->w; }\n",
    "src/x/4.c": "int d(Wide *w) { return w->w; }\n",
}


def project(tmp_path: Path) -> Path:
    files = {"include/COMMON/common.h": COMMON, "docs/MODULES.tsv": MAP, **UNITS}
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def row(found: dict, name: str) -> dict:
    return next(r for r in found["rows"] if name in r["names"])


def test_a_value_declaration_belongs_to_the_module_of_its_definer(tmp_path: Path) -> None:
    found = owners.run(project(tmp_path), "include/COMMON", packs=tmp_path / "out")
    score = row(found, "gScore")
    assert (
        score["owner"] == "src/game/MENU/A.CPP" and score["reason"] == "definer" and score["to"] == "include/MENU/A.H"
    )
    assert row(found, "Menu_Draw")["owner"] == "src/game/MENU/A.CPP"


def test_a_type_or_macro_belongs_to_its_majority_user_and_a_widely_used_one_stays_shared(tmp_path: Path) -> None:
    found = owners.run(project(tmp_path), "include/COMMON")
    assert row(found, "Menu")["owner"] == "src/game/MENU/A.CPP" and row(found, "Menu")["reason"] == "majority user"
    assert row(found, "MENU_MAX")["owner"] == "src/game/MENU/A.CPP" and row(found, "MENU_MAX")["kind"] == "macro"
    assert row(found, "Wide")["owner"] == "" and row(found, "Wide")["reason"] == "shared by 4 modules"
    assert row(found, "Pad")["owner"] in (
        "src/game/MENU/A.CPP",
        "src/game/PLAY/P.CPP",
    )  # used by two modules: the larger user count wins
    assert row(found, "gNobody")["reason"] == "unused"
    assert not any("COMMON_H" in r["names"] for r in found["rows"])  # the include guard is no declaration


def test_the_packs_hold_the_move_list(tmp_path: Path) -> None:
    out = tmp_path / "out"
    found = owners.run(project(tmp_path), "include/COMMON", packs=out)
    lines = (out / "owners.tsv").read_text().splitlines()
    assert lines[0].startswith("header\tline\tkind") and len(lines) == found["declarations"] + 1
    assert found["owned"] >= 4 and found["by_owner"]["src/game/MENU/A.CPP"] >= 3


def test_move_packs_move_the_owned_declarations_and_keep_the_include_order(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "include/MENU").mkdir(parents=True)
    (root / "include/MENU/A.H").write_text("#ifndef MENU_A_H\n#define MENU_A_H\n#endif\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    found = owners.run(root, "include/COMMON", packs=tmp_path / "out")
    packs = found["move_packs"]
    assert packs and packs[0]["owner"] == "src/game/MENU/A.CPP" and packs[0]["to"] == "include/MENU/A.H"
    diff = tmp_path / "out" / packs[0]["pack"]
    subprocess.run(["git", "-C", str(root), "apply", "--check", str(diff)], check=True)
    subprocess.run(["git", "-C", str(root), "apply", str(diff)], check=True)
    common = (root / "include/COMMON/common.h").read_text()
    menu = (root / "include/MENU/A.H").read_text()
    assert common.count('#include "MENU/A.H"') == 1 and "extern int gScore;" not in common
    assert "extern int gScore;" in menu and menu.rstrip().endswith("#endif")
    assert common.index('#include "MENU/A.H"') < common.index(
        "extern int gNobody;"
    )  # the include stands where the first moved one was


def test_the_units_are_those_under_the_layouts_source_roots(tmp_path: Path) -> None:
    root = project(tmp_path)
    before = owners.sources(root)
    (root / "sdk").mkdir()
    (root / "sdk" / "os.c").write_text("int os(void) { return 0; }\n")
    (root / "config.toml").write_text('[layout]\nsource_roots = ["src", "sdk"]\n')
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert owners.sources(root) == sorted([*before, "sdk/os.c"])
