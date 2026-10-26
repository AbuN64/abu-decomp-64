import json
import subprocess

from abu_decomp.data import fntry
from abu_decomp.data.config import DataConfig
from abu_decomp.data.trial import Builder

CFG = DataConfig(
    versions=("a", "b"),
    reference="a",
    make=("make",),
    object="build/{v}/{unit}.o",
    fncmp=("cmp.py", "--aligned", "{v}", "{fn}", "{obj}"),
)


def tree(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/u.c").write_text("ORIG")
    for v in "ab":
        (tmp_path / f"docs/map-{v}.json").write_text(json.dumps({"fns": [["f", 0, 0, 50, "src/u.c"]]}))
    return tmp_path


def test_candidate_is_built_scored_and_the_unit_restored(tmp_path):
    t = tree(tmp_path)
    cand = tmp_path / "cand.c"
    cand.write_text("NEW")
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, (t / "src/u.c").read_text()))
        out = "edit=0\n" if "cmp.py" in cmd[0] else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    rows = fntry.try_fn(Builder(CFG, t, run), "f", ["a", "b"], cand)
    assert [r["edit"] for r in rows] == [0, 0]
    assert all(text == "NEW" for _, text in seen)
    assert (t / "src/u.c").read_text() == "ORIG"


def test_failed_build_reports_log_and_restores(tmp_path):
    t = tree(tmp_path)

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if cmd[-1].endswith(".o") else 0, "", "boom")

    rows = fntry.try_fn(Builder(CFG, t, run), "f", ["a"], None)
    assert rows[0]["edit"] is None and "boom" in rows[0]["log"]
    assert (t / "src/u.c").read_text() == "ORIG"


def test_verbose_puts_v_before_the_positionals(tmp_path):
    t = tree(tmp_path)
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "edit=3\n", "")

    fntry.try_fn(Builder(CFG, t, run), "f", ["a"], None, verbose=True)
    assert seen[-1][:4] == ["cmp.py", "--aligned", "-v", "a"]


def test_a_function_that_matches_but_breaks_an_exact_neighbour_refuses(tmp_path):
    """PORT-BIG-2: INCLUDE_ASM -> C matched Pak_HandleMenuAction and shifted the text order under its neighbour."""
    t = tree(tmp_path)
    for v in "ab":
        rows = [["f", 0, 0, 100, "src/u.c"], ["g", 4, 0, 100, "src/u.c"], ["h", 8, 0, 50, "src/u.c"]]
        (t / f"docs/map-{v}.json").write_text(json.dumps({"fns": rows}))

    def run(cmd, **kw):
        edit = 7 if "g" in cmd else 0
        return subprocess.CompletedProcess(cmd, 0, f"edit={edit}\n" if "cmp.py" in cmd[0] else "", "")

    rows = fntry.try_fn(Builder(CFG, t, run), "f", ["a"], None)
    assert rows[0]["edit"] == 0 and rows[0]["broken"] == [("g", "edit=7")]
