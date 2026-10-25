from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from abu_decomp import DecompError, diffonto


def g(tree: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t", *a],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def repo(tmp_path: Path) -> Path:
    t = tmp_path / "t"
    t.mkdir()
    g(t, "init", "-q", "-b", "main")
    (t / "src").mkdir()
    (t / "src/a.c").write_text("".join(f"a{i}\n" for i in range(30)))
    (t / "src/b.c").write_text("b\n")
    for v in ("us", "eu"):
        (t / f"versions/{v}").mkdir(parents=True)
        (t / f"versions/{v}/symbol_addrs.txt").write_text("func_1 = 0x80000000; // x\nother = 0x80000010;\n")
    g(t, "add", "-A")
    g(t, "commit", "-qm", "base")
    return t


def test_change_is_rebuilt_on_newer_main_and_tree_is_untouched(tmp_path):
    t = repo(tmp_path)
    # main moves on the far end of the same file, and the lane tree also carries unrelated work in b.c
    g(t, "branch", "newmain")
    g(t, "checkout", "-q", "newmain")
    (t / "src/a.c").write_text("".join(f"a{i}\n" for i in range(30)).replace("a28\n", "moved\n"))
    g(t, "commit", "-qam", "main moved")
    g(t, "checkout", "-q", "main")
    (t / "src/a.c").write_text((t / "src/a.c").read_text().replace("a2\n", "A2\n"))  # lane edit near the top
    (t / "src/b.c").write_text("unrelated\n")
    head = g(t, "rev-parse", "HEAD")
    out = tmp_path / "out.diff"
    r = diffonto.run(t, "newmain", ["src/a.c"], msg="fix a2", out=out)
    text = out.read_text()
    assert text.startswith("# MSG: fix a2\n") and "+A2" in text and "moved" not in text and "unrelated" not in text
    assert g(t, "rev-parse", "HEAD") == head and (t / "src/b.c").read_text() == "unrelated\n"
    assert g(t, "show", f"{r['probe']}:src/a.c").count("moved") == 1 and "A2" in g(t, "show", f"{r['probe']}:src/a.c")


def test_hunk_filter_and_symbol_rename(tmp_path):
    t = repo(tmp_path)
    (t / "src/a.c").write_text((t / "src/a.c").read_text().replace("a2\n", "KEEP\n").replace("a25\n", "SKIP\n"))
    r = diffonto.run(t, "HEAD", ["src/a.c"], hunks="KEEP", rename="func_1=Nice_Name", out=tmp_path / "o.diff")
    text = (tmp_path / "o.diff").read_text()
    assert "+KEEP" in text and "SKIP" not in text and "+Nice_Name = 0x80000000" in text
    assert sorted(r["renamed"]) == ["versions/eu/symbol_addrs.txt", "versions/us/symbol_addrs.txt"]


def test_refusals(tmp_path):
    t = repo(tmp_path)
    with pytest.raises(DecompError, match="no change"):
        diffonto.run(t, "HEAD", ["src/a.c"])
    (t / "src/a.c").write_text("x\n")
    with pytest.raises(DecompError, match="matching"):
        diffonto.run(t, "HEAD", ["src/a.c"], hunks="nomatch")
    with pytest.raises(DecompError, match="starts no line"):
        diffonto.run(t, "HEAD", [], rename="missing=New")


def test_function_filter_keeps_one_functions_hunks_and_its_rows(tmp_path):
    t = repo(tmp_path)
    src = (
        "int Other(void)\n{\n    return 1;\n}\n\n"
        "int Foo_Bar(int x)\n{\n    if (x) {\n        return 2;\n    }\n    return 3;\n}\n\n"
        "int Third(void)\n{\n    return 4;\n}\n"
    )
    (t / "src/f.c").write_text(src)
    g(t, "add", "-A")
    g(t, "commit", "-qm", "add f")
    new = src.replace("return 1;", "return 11;").replace("return 3;", "return 33;").replace("return 4;", "return 44;")
    (t / "src/f.c").write_text(new)
    (t / "versions/us/symbol_addrs.txt").write_text("func_1 = 0x80000000; // x\nFoo_Bar = 0x80000010;\n")
    out = tmp_path / "o.diff"
    diffonto.run(t, "HEAD", ["src/f.c", "versions"], function="Foo_Bar", out=out)
    text = out.read_text()
    assert "+    return 33;" in text and "+Foo_Bar = 0x80000010;" in text
    assert "return 11" not in text and "return 44" not in text
    with pytest.raises(DecompError):
        diffonto.run(t, "HEAD", ["src/f.c"], function="Nope_Fn")


def test_a_conflict_names_the_files_and_what_to_do(tmp_path):
    t = repo(tmp_path)
    g(t, "branch", "newmain")
    g(t, "checkout", "-q", "newmain")
    (t / "src/a.c").write_text((t / "src/a.c").read_text().replace("a2\n", "main2\n"))
    g(t, "commit", "-qam", "main moved")
    g(t, "checkout", "-q", "main")
    (t / "src/a.c").write_text((t / "src/a.c").read_text().replace("a2\n", "lane2\n"))
    with pytest.raises(DecompError) as got:
        diffonto.run(t, "newmain", ["src/a.c"])
    assert "src/a.c" in str(got.value) and "fresh lease" in str(got.value)
    assert diffonto.conflict_message("error: something odd\n", "M").startswith("the change does not apply onto M")
