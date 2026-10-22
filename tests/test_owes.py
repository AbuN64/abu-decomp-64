from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from abu_decomp import evidence_gate, owes

NM_FILE = (
    "#ifdef NON_MATCHING\nint func_80000010_de(int a) { return a; }\n#else\n"
    'INCLUDE_ASM("o", func_80000010_de);\n#endif\n'
)
CFG = {"suffixes": (".c",), "guard": "NON_MATCHING", "stand_in": "INCLUDE_ASM", "headers": ["include"], "roms": []}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "rw"
    (root / "src").mkdir(parents=True)
    (root / "include").mkdir()
    (root / "src" / "a.c").write_text(NM_FILE)
    (root / "src" / "b.c").write_text('INCLUDE_ASM("o", func_80000020_de);\nINCLUDE_ASM("o", func_80000030_de);\n')
    (root / "src" / "c.c").write_text("int func_80000040_de(void) { return 1; }\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    return root


def run_with(
    tmp_path: Path, project: Path, monkeypatch, scores: dict[str, tuple[int, int]] | None = None, stale: str = NM_FILE
):
    runs = tmp_path / "runs"
    for number, names in enumerate([["func_80000020_de", "func_80000010_de"], ["func_80000010_de"]]):
        log = runs / "plays" / f"r{number}" / "output.txt"
        log.parent.mkdir(parents=True)
        log.write_text("".join(f"boot-step: {i}/9 {n}\n" for i, n in enumerate(names)))
    work = tmp_path / "work"
    (work / "evidence").mkdir(parents=True)
    for function, (k, n) in (scores or {}).items():
        text = stale
        facts = {"function": function, "text": evidence_gate.text_digest(text)}
        listing = subprocess.run(
            ["git", "-C", str(project), "ls-tree", "-r", "HEAD", "--", "include"], capture_output=True, text=True
        ).stdout
        facts |= {"headers": evidence_gate.header_digest(listing), "roms": evidence_gate.rom_digest(project, [])}
        (work / "evidence" / "m.json").write_text(json.dumps({"k": k, "n": n, "sha": "h", "note": "", **facts}))
    import abu_decomp.behave.run as run_module
    import abu_decomp.tas as tas_module

    monkeypatch.setattr(run_module, "configuration", lambda profile: {})
    monkeypatch.setattr(run_module, "work_dir", lambda config: work)
    monkeypatch.setattr(tas_module, "settings", lambda profile: {"runs": runs})
    monkeypatch.setattr(owes, "settings", lambda profile: CFG)
    profile = SimpleNamespace(source_file=Path("p.json"))
    monkeypatch.setattr("abu_decomp.tools.project_dir", lambda profile, given=None: project)
    return owes.run(profile, None, 10)


def test_lists_guarded_bodies_and_reached_stand_ins_ranked_by_runs(tmp_path, project, monkeypatch):
    got = run_with(tmp_path, project, monkeypatch)
    assert got["owes"] == 2 and got["runs"] == 2 and got["levels"] == {"asm": 1, "C unproven": 1, "behaves": 0}
    assert [(r["function"], r["level"], r["reached"]) for r in got["top"]] == [
        ("func_80000010_de", "C unproven", 2),
        ("func_80000020_de", "asm", 1),
    ]
    assert got["next"] == "recipe work func_80000010_de"  # an unreached stand-in (30) and a plain body (40) owe nothing


def test_a_cached_measure_of_the_file_as_it_stands_gives_the_behaves_level(tmp_path, project, monkeypatch):
    got = run_with(tmp_path, project, monkeypatch, {"func_80000010_de": (7, 48)})
    assert got["levels"]["behaves"] == 1 and got["top"][0]["level"] == "behaves 7/48"


def test_a_measure_of_other_text_is_not_a_level(tmp_path, project, monkeypatch):
    (project / "src" / "a.c").write_text(NM_FILE.replace("return a;", "return a + 2;"))
    got = run_with(tmp_path, project, monkeypatch, {"func_80000010_de": (7, 48)}, NM_FILE)
    assert got["levels"]["behaves"] == 0


def test_a_function_waiting_for_a_type_item_ranks_by_that_items_command(tmp_path, project, monkeypatch):
    needs = project / ".unbake" / "recipe" / "needs.json"
    needs.parent.mkdir(parents=True)
    needs.write_text(
        json.dumps(
            {
                "func_80000010_de": {
                    "item": "func_80000010_de.arg0",
                    "next": "recipe work func_80000010_de.arg0 --kind type",
                }
            }
        )
    )
    got = run_with(tmp_path, project, monkeypatch)
    assert got["next"] == "recipe work func_80000010_de.arg0 --kind type"
    assert got["top"][0]["waits"] == "func_80000010_de.arg0" and "waits" not in got["top"][1]
    assert got["top"][1]["next"] == "recipe work func_80000020_de"
