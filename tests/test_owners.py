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
