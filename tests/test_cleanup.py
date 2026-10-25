import subprocess

from abu_decomp import cleanup


def test_count_text_uses_the_one_pattern_set():
    got = cleanup.count_text("func_80001234 D_80001234 unk4 pad0C sp1C func_1234 Foo_unk spam D_8000123G")
    assert dict(got) == {"func": 1, "data": 1, "unk": 1, "pad": 1, "sp": 1}


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
