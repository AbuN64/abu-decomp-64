import argparse
import json
import subprocess

import pytest

from abu_decomp import DecompError, cli, filesets, owners

TREE = {
    "src/A/a1.c": '#include "a.h"\n#include "util.h"\n#include "shared.h"\n',
    "src/A/a2.c": '#include "a.h"\n',
    "src/B/b1.c": '#include "b.h"\n#include "util.h"\n#include "shared.h"\n',
    "src/C/c1.c": '#include "../B/b.h"\n',
    "src/A/a.h": "",
    "src/B/b.h": "",
    "include/util.h": "",
    "include/shared.h": '#include "deep.h"\n',
    "include/deep.h": "",
    "include/orphan.h": "",
}


def sets(unit_module=None, module_header=None, lanes=("L1", "L2"), kept=None):
    return filesets.partition(
        {p: len(t) + 1 for p, t in TREE.items()}, TREE, unit_module or {}, module_header or {}, lanes, kept
    )


def lane_of(doc, path):
    return next(lane for lane, got in doc["lanes"].items() if path in got["files"])


def test_every_file_is_in_exactly_one_set_and_a_shared_header_is_globals():
    doc = sets()
    assert filesets.check(doc, {p: 1 for p in TREE}) == []
    assert doc["globals"]["include/shared.h"] == ["dir:src/A", "dir:src/B"]
    assert doc["globals"]["include/deep.h"] == ["dir:src/A", "dir:src/B"]  # read through shared.h
    assert sum(len(got["files"]) for got in doc["lanes"].values()) + len(doc["globals"]) == len(TREE)


def test_a_header_one_module_reads_goes_with_it_and_a_module_header_with_its_module():
    doc = sets(unit_module={"src/C/c1.c": "m_b"}, module_header={"m_b": "src/B/b.h"})
    assert lane_of(doc, "src/C/c1.c") == lane_of(doc, "src/B/b.h") == doc["modules"]["m_b"]
    assert "include/util.h" in doc["globals"]
    assert lane_of(doc, "src/A/a.h") == lane_of(doc, "src/A/a1.c") == lane_of(doc, "src/A/a2.c")


def test_a_module_keeps_its_lane_from_the_last_run_and_check_names_a_duplicate():
    doc = sets(kept={"dir:src/A": "L2", "dir:src/B": "L2"})
    assert lane_of(doc, "src/A/a1.c") == lane_of(doc, "src/B/b1.c") == "L2"
    bad = {"lanes": {"L1": {"files": ["x"]}, "L2": {"files": ["x"]}}, "globals": {}}
    assert filesets.check(bad, {"x": 1, "y": 1}) == ["x is in 2 sets", "y is in no set"]


def test_write_then_load_and_a_gone_lane_is_removed(tmp_path):
    doc = sets()
    filesets.write(tmp_path, doc)
    (filesets.directory(tmp_path) / "OLD.json").write_text("{}")
    filesets.write(tmp_path, doc)
    assert not (filesets.directory(tmp_path) / "OLD.json").exists()
    got = filesets.load(tmp_path, "L1")
    assert got and got["lane"] == "L1" and filesets.load(tmp_path, "nope") is None
    assert json.loads((filesets.directory(tmp_path) / "GLOBALS.json").read_text())["lane"] == "GLOBALS"
    assert filesets.kept(tmp_path) == doc["modules"]


def git_tree(root):
    for path, text in TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


@pytest.mark.parametrize("with_map", [True, False])
def test_build_reads_the_module_map_or_falls_back_to_the_folder_tree(tmp_path, with_map):
    git_tree(tmp_path)
    (tmp_path / "MODULES.tsv").write_text("unit\tmodule\theader\nsrc/C/c1.c\tm_b\tsrc/B/b.h\nsrc/B/b1.c\tm_b\t-\n")
    doc = filesets.build(tmp_path, tmp_path / "names", 2, "MODULES.tsv" if with_map else "absent.tsv")
    assert doc["files"] == len(TREE) and filesets.check(doc, {p: 1 for p in TREE}) == []
    assert (doc["map"] is not None) == with_map
    if with_map:
        assert lane_of(doc, "src/C/c1.c") == lane_of(doc, "src/B/b.h") == lane_of(doc, "src/B/b1.c")


def test_show_prints_the_file_set_and_refuses_a_lane_with_neither_view(tmp_path, capsys):
    filesets.write(tmp_path, sets())
    args = argparse.Namespace(verb="slices", show="L1", names=str(tmp_path), book=None)
    document, code, _ = cli._run_cleanup(args)
    assert code == 0 and document["file_set"]["files"] and "file set" in capsys.readouterr().err
    args.show = "NOPE"
    with pytest.raises(DecompError, match="no slice for lane NOPE"):
        cli._run_cleanup(args)


def test_the_map_reader_and_the_partition_agree_on_module_headers(tmp_path):
    (tmp_path / "m.tsv").write_text("unit\tmodule\theader\nsrc/A/a1.c\tm\tsrc/A/a.h\n")
    unit_module, module_header = owners.read_map(tmp_path / "m.tsv")
    doc = sets(unit_module, module_header)
    assert lane_of(doc, "src/A/a.h") == doc["modules"]["m"]
