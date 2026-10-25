import subprocess

from abu_decomp import cleanup


def test_count_text_uses_the_one_pattern_set():
    got = cleanup.count_text("func_80001234 D_80001234 unk4 pad0C sp1C func_1234 Foo_unk spam D_8000123G")
    assert dict(got) == {"func": 1, "data": 1, "unk": 1, "pad": 1, "sp": 1, "var": 0, "arg": 0}


def test_decompiler_locals_and_unnamed_parameters_are_counted():
    got = cleanup.count_text("var_s0 = temp_f2 + arg0; var_a1; argument arg12 arg; x_var_s0 temp_")
    assert got["var"] == 3 and got["arg"] == 2


def test_evidence_asks_each_step_for_the_classes_a_folder_holds_and_skips_the_rest():
    asked = []

    def ask(step, target):
        asked.append((step, target))
        return {"L": {"slots": 9, "no_evidence": 4}, "N4": {"targets": 3, "no_evidence": 1}}.get(step, {})

    folders = {
        "game/A": {"sp": 2, "var": 0, "arg": 0, "data": 1, "func": 0},
        "include/COMMON": {"sp": 5},
        "game/B": {"func": 4},
    }
    got = cleanup.evidence(folders, ask)
    assert got["game/A"] == {"locals": {"namable": 9, "no_evidence": 4}, "data": {"namable": 3, "no_evidence": 1}}
    assert got["include/COMMON"] == {} and got["game/B"] == {"functions": {"namable": 0, "no_evidence": 0}}
    assert asked == [("L", "src/game/A"), ("N4", "src/game/A"), ("N5", "src/game/B")]


def test_count_groups_by_folder_and_filters_by_dir_or_leaf(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    for path, text in {
        "src/game/A/x.c": "func_80000001 func_80000002 D_80000003",
        "src/game/B/y.CPP": "unk4 pad0C",
        "include/COMMON/z.H": "sp1C",
        "src/game/A/note.txt": "func_80000009",
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    git("add", "-A")
    got = cleanup.count(tmp_path)
    assert got["total"] == 6 and got["folders"]["game/A"]["count"] == 3 and got["folders"]["include/COMMON"]["sp"] == 1
    assert list(cleanup.count(tmp_path, "src/game/A")["folders"]) == ["game/A"]
    assert list(cleanup.count(tmp_path, "B")["folders"]) == ["game/B"]
