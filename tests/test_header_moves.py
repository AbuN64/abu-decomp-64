"""Real guardchanged delete/add regression: file identity is shared by gate and remap."""

import difflib
import json

import pytest

from abu_decomp import header_moves, remap
from abu_decomp.data import gate
from abu_decomp.data.config import DataConfig

OLD = "include/DATA/OVLDAT6/800CF230.H"
NEW = "include/DATA/OVLDAT6/PLAYERAI_INITIALIZEPLAYERHEALTH.H"
BEFORE = (
    "#ifndef DATA_OVLDAT6_800CF230_H\n#define DATA_OVLDAT6_800CF230_H\n"
    '#include "COMMON/OBJRECV5.H"\nextern float D_800C9FEC;\n#endif\n'
)
AFTER = BEFORE.replace("DATA_OVLDAT6_800CF230_H", "DATA_OVLDAT6_PLAYERAI_INITIALIZEPLAYERHEALTH_H")


def change(path, before, after):
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            "a/" + path if before else "/dev/null",
            "b/" + path if after else "/dev/null",
            n=0,
        )
    )


def moved(after=AFTER):
    return change(OLD, BEFORE, "") + change(NEW, "", after)


def test_real_header_move_is_exact_and_reconciles_only_that_baseline(tmp_path):
    patch = moved()
    assert header_moves.correspondence(patch) == ({OLD: NEW}, "")
    census = tmp_path / "census.json"
    census.write_text(
        json.dumps({"clusters": [], "lones": [{"sym": "D_800C9FEC", "class": "SCALAR", "access": ["lw:i32"]}]})
    )
    cfg = DataConfig()
    assert gate.check_diff(cfg, census, patch) == []
    extra = change("include/unrelated.h", "", "extern float D_800C9FEC;\n")
    assert len(gate.check_diff(cfg, census, patch + extra)) == 1
    assert "KIND D_800C9FEC" in gate.check_diff(cfg, census, extra)[0]
    # A moved declaration changed from int to float must still fail KIND.
    retyped = change(OLD, BEFORE.replace("float", "int"), "") + change(NEW, "", AFTER)
    assert "KIND D_800C9FEC" in gate.check_diff(cfg, census, retyped)[0]


@pytest.mark.parametrize(
    "after",
    [
        AFTER.replace("float", "int"),
        AFTER.replace("OBJRECV5", "OTHER"),
        AFTER.replace("extern float", "extern const float"),
        AFTER.replace("#endif", "#else\nextern int other;\n#endif"),
        AFTER.replace("extern", "/* changed */ extern"),
    ],
)
def test_changed_type_include_activation_or_content_never_pairs(after):
    patch = moved(after)
    assert header_moves.correspondence(patch) == ({}, patch)


def test_duplicate_or_incomplete_evidence_does_not_choose_a_successor():
    for patch in (
        moved() + change("include/copy.h", "", AFTER),
        moved() + change("include/twin.h", BEFORE, ""),
        moved().replace("@@ -0,0 +1,5 @@", "@@ -0,0 +2,5 @@"),
        moved().replace("+extern float D_800C9FEC;\n", ""),
        change(NEW, "", AFTER),
        change(OLD, BEFORE, ""),
    ):
        assert header_moves.correspondence(patch) == ({}, patch)


def test_remap_uses_one_cohort_diff_and_keeps_unexplained_deletion(tmp_path):
    calls = []

    def run(argv, cwd, env, timeout):
        calls.append(argv)
        if "--name-status" in argv:
            return 0, f"D\0{OLD}\0A\0{NEW}\0D\0include/lost.h\0", ""
        assert "--no-renames" in argv
        return 0, moved(), ""

    assert remap.moves(run, tmp_path, "old", "new") == {OLD: NEW, "include/lost.h": None}
    assert len(calls) == 2
    facts = tmp_path / "gates.tsv"
    facts.write_text(f"kind\t{OLD}\tD_800C9FEC\n")
    mapping = {OLD: NEW}
    assert remap.carry_moves(tmp_path, ["gates.tsv"], mapping, write=False)["files"]
    assert OLD in facts.read_text()
    remap.carry_moves(tmp_path, ["gates.tsv"], mapping)
    assert facts.read_text() == f"kind\t{NEW}\tD_800C9FEC\n"
    assert not remap.carry_moves(tmp_path, ["gates.tsv"], mapping)["files"]
