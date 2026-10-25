"""decomp data: sizeof probe, census on a tiny tree, gate, check/mixed loop, probe scoring, CLI (compiler and make mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from abu_decomp import DecompError, cli
from abu_decomp.data import census, check, config, gate, sizeof, source, trial

CFG = config.parse({
    "reference": "r1", "versions": ["r1", "r2"], "cc1": "/fake/cc1", "cpp": ["cpp"], "make": ["make"], "fncmp": ["cmp", "{v}", "{fn}", "{obj}"],
})  # fmt: skip


def fake_run(sizes: dict[str, int], scores: dict[str, int] | None = None, build_rc: int = 0):
    """A subprocess.run stand-in: cpp prints nothing, cc1 prints .comm for each typeof probe, make succeeds, cmp scores."""
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == "/fake/cc1":
            probes = [
                ln.split()[-1].removeprefix("probe_").rstrip(";") for ln in kw["input"].splitlines() if "probe_" in ln
            ]
            out = "".join(f".comm probe_{s},{sizes[s]}\n" for s in probes if s in sizes)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[0] == "cmp":
            sc = (scores or {}).get(cmd[2], 0)
            return subprocess.CompletedProcess(cmd, 0, f"edit={sc}\n", "")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, "src/a.c\nREADME\n", "")
        return subprocess.CompletedProcess(cmd, build_rc, "", "error: boom" if build_rc else "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def mini_tree(tmp_path: Path) -> Path:
    t = tmp_path
    for v in ("r1", "r2"):
        (t / "versions" / v).mkdir(parents=True)
        (t / "versions" / v / "symbol_addrs.txt").write_text(
            "D_80100000 = 0x80100000;\nD_80100004 = 0x80100004;\nD_80100008 = 0x80100008;\nfunc_a = 0x80000000;\n"
        )
        (t / "asm" / v / "data").mkdir(parents=True)
        (t / "asm" / v / "nonmatchings" / "x").mkdir(parents=True)
        (t / "asm" / v / "nonmatchings" / "x" / "func_a.s").write_text(
            "/* 0 80000000 3C018010 */ lui $at, %hi(D_80100000)\n"
            "/* 4 80000004 8C2A0004 */ lw $t2, %lo(D_80100000+4)($at)\n"
            "/* 8 80000008 8C2B0000 */ lw $t3, %lo(D_80100008)($at)\n"
        )
    (t / "include").mkdir()
    (t / "include" / "T.H").write_text(
        "typedef int s32;\ntypedef struct { s32 a; s32 b; } Pair;\nextern Pair D_80100000;\n"
    )
    (t / "src").mkdir()
    (t / "src" / "a.c").write_text(
        '#include "T.H"\nextern s32 D_80100004;\nextern s32 D_80100008;\nvoid func_a(void) {\n    D_80100004 = 1;\n}\n'
    )
    (t / "docs").mkdir()
    (t / "docs" / "map-r1.json").write_text(json.dumps({"fns": [["func_a", 0, 12, 50.0, "src/a.c", 1]]}))
    return t


def test_comm_sizes_and_probe_all_per_version(tmp_path: Path):
    decl = {"D_80100000": [("include/T.H", "Pair")], "D_1": [("include/T.H", "s32")]}
    run = fake_run({"D_80100000": 8})
    sizes, where = sizeof.probe_all(CFG, tmp_path, decl, {}, ["r1", "r2"], run)
    assert sizes == {"D_80100000": {"r1": 8, "r2": 8}} and where["D_80100000"] == "decl:include/T.H"
    assert sizeof.stable(sizes["D_80100000"])
    assert sizeof.stable({"r1": 8, "r2": 4}) is False


def test_census_on_mini_tree_finds_member_and_path_and_queue(tmp_path: Path):
    t = mini_tree(tmp_path)
    out = tmp_path / "out"
    st = census.run(CFG, t, out, runner=fake_run({"D_80100000": 8}))
    data = json.loads((out / "census.json").read_text())
    member = next(m for c in data["clusters"] for m in c["members"] if m["sym"] == "D_80100004")
    assert member["inner"] == "D_80100000+0x4" and member["path"] == "D_80100000.b"
    assert st["by_class"]["SCALAR_MEMBER"] == 1 and st["paths_resolved"] == 1
    queue = (out / "QUEUE.md").read_text()
    assert "| SCALAR_MEMBER | D_80100000 |" in queue
    # regeneration carries a hand-set status over by base symbol
    (out / "QUEUE.md").write_text(queue.replace("| B | - |", "| B | LANDED abc1234 |"))
    census.run(CFG, t, out, runner=fake_run({"D_80100000": 8}))
    assert "LANDED abc1234" in (out / "QUEUE.md").read_text()


def test_census_flags_unstable_sizeof(tmp_path: Path):
    t = mini_tree(tmp_path)
    calls = iter([8, 4])

    def run(cmd, **kw):
        if cmd[0] == "/fake/cc1":
            return subprocess.CompletedProcess(cmd, 0, f".comm probe_D_80100000,{next(calls)}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    st = census.run(CFG, t, tmp_path / "o", runner=run)
    assert st["sizeof_unstable"] == ["D_80100000"]


def census_json(tmp_path: Path) -> Path:
    t = mini_tree(tmp_path / "tree")
    out = tmp_path / "c"
    census.run(CFG, t, out, runner=fake_run({"D_80100000": 8}))
    return out / "census.json"


def test_gate_diff_member_kind_literal_and_mixed(tmp_path: Path):
    cj = census_json(tmp_path)
    diff = "+++ b/src/b.c\n+extern s32 D_80100004;\n"
    assert gate.check_diff(CFG, cj, diff)[0].startswith("src/b.c: MEMBER D_80100004")
    assert gate.check_diff(CFG, cj, "+++ b/src/b.c\n+extern s32 D_80100004; /* MIXED */\n") == []
    retype = "--- a/src/b.c\n+++ b/src/b.c\n-extern u8 D_80100004;\n+extern s32 D_80100004;\n"
    assert gate.check_diff(CFG, cj, retype) == []
    kind = "+++ b/src/b.c\n+extern f32 D_80100008;\n"
    assert "KIND D_80100008" in gate.check_diff(CFG, cj, kind)[0]
    assert gate.check_diff(CFG, cj, "+++ b/README\n+extern s32 D_80100004;\n") == []


def test_gate_ratchet_counts_and_baseline(tmp_path: Path):
    cj = census_json(tmp_path)
    tree = tmp_path / "tree"
    assert gate.count_tree(CFG, cj, tree) == 1
    base = tmp_path / "b.json"
    assert gate.ratchet(1, base, True) == [] and json.loads(base.read_text()) == {"raw_member_scalar_decls": 1}
    assert gate.ratchet(1, base, False) == [] and gate.ratchet(0, base, False) == []
    assert "RATCHET" in gate.ratchet(2, base, False)[0]


def test_builder_score_and_build_failure(tmp_path: Path):
    (tmp_path / "src").mkdir()
    b = trial.Builder(CFG, tmp_path, fake_run({}, {"func_a": 7}))
    assert b.score("r1", "func_a", "src/a.c") == 7
    assert (
        trial.build_score(trial.Builder(CFG, tmp_path, fake_run({}, build_rc=2)), "r1", "func_a", "src/a.c", "int x;\n")
        is None
    )


def test_probe_restores_unit_and_reports(tmp_path: Path):
    t = mini_tree(tmp_path)
    orig = (t / "src" / "a.c").read_text()
    census_d = {"clusters": [], "lones": [{"sym": "D_80100008", "decl_kind": ["i"], "literal": False}]}
    (t / "docs" / "map-r1.json").write_text(json.dumps({"fns": [["func_a", 0, 12, 50.0, "src/a.c", 1]]}))
    src = (t / "src" / "a.c").read_text().replace("D_80100004 = 1;", "D_80100008 = 1;")
    (t / "src" / "a.c").write_text(src)
    b = trial.Builder(CFG, t, fake_run({}, {"func_a": 5}))
    line = trial.probe_fn(b, "r1", "func_a", "src/a.c", census_d)
    assert line.startswith("func_a base=5 final=5 kept=[]") and "tried=1" in line
    assert (t / "src" / "a.c").read_text() == src and orig != src


def test_check_lists_broken_exact_fns_and_mixed_puts_back(tmp_path: Path):
    t = mini_tree(tmp_path)
    (t / "docs" / "map-r1.json").write_text(json.dumps({"fns": [["func_a", 0, 12, 100.0, "src/a.c", 1]]}))
    b = trial.Builder(CFG, t, fake_run({}, {"func_a": 3}))
    res = check.check(b, "r1", ["src/a.c"])
    assert res["built"] and res["broken"] == [["src/a.c", "func_a", "edit=3"]]
    ok = check.check(trial.Builder(CFG, t, fake_run({}, {"func_a": 0})), "r1", ["src/a.c"])
    assert ok["broken"] == []
    assert check.check(trial.Builder(CFG, t, fake_run({}, build_rc=2)), "r1", ["src/a.c"])["built"] is False

    (t / "src" / "a.c").write_text('#include "T.H"\nvoid func_a(void) {\n    D_80100000.b = 1;\n}\n')
    state = {"n": 0}
    base = fake_run({})

    def run(cmd, **kw):
        if cmd[0] == "cmp":
            broken = "D_80100000.b" in (t / "src" / "a.c").read_text()
            return subprocess.CompletedProcess(cmd, 0, f"edit={3 if broken else 0}\n", "")
        state["n"] += 1
        return base(cmd, **kw)

    res = check.mixed_loop(trial.Builder(CFG, t, run), "r1", "D_80100004", "D_80100000.b", "s32", ["src/a.c"])
    assert res["broken"] == [] and res["mixed"] == ["MIXED D_80100004 func_a src/a.c"]
    assert "D_80100004 = 1;" in (t / "src" / "a.c").read_text()


def test_unwrap_used_by_try_edits(tmp_path: Path):
    t = mini_tree(tmp_path)
    b = trial.Builder(CFG, t, fake_run({}, {"func_a": 2}))
    assert trial.try_edits(b, "r1", "func_a", "src/a.c", [("D_80100004 = 1", "D_80100004 = 2")]) == "func_a edit=2"
    assert "NOMATCH" in trial.try_edits(b, "r1", "func_a", "src/a.c", [("zzz", "y")])
    assert "D_80100004 = 1" in (t / "src" / "a.c").read_text()
    assert source.unwrap("x", "f") == "x"


def run_cli(args: list[str], capsys) -> tuple[int, dict]:
    code = cli.main(["data", *args])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(out)


def test_cli_gate_refuses_and_passes(tmp_path: Path, capsys):
    cj = census_json(tmp_path)
    cfgf = tmp_path / "d.toml"
    cfgf.write_text('[data]\nreference = "r1"\nversions = ["r1", "r2"]\n')
    bad = tmp_path / "bad.diff"
    bad.write_text("+++ b/src/b.c\n+extern s32 D_80100004;\n")
    code, env = run_cli(["--config", str(cfgf), "gate", "--census", str(cj), "--diff", str(bad)], capsys)
    assert code == 1 and env["findings"][0]["key"] == "data.gate" and not env["ok"]
    good = tmp_path / "good.diff"
    good.write_text("+++ b/src/b.c\n+extern s32 D_80100099;\n")
    code, env = run_cli(["--config", str(cfgf), "gate", "--census", str(cj), "--diff", str(good)], capsys)
    assert code == 0 and env["result"]["violations"] == []


def test_cli_rewrite_reads_expression_from_census(tmp_path: Path, capsys):
    cj = census_json(tmp_path)
    tree = tmp_path / "tree"
    cfgf = tmp_path / "d.toml"
    cfgf.write_text('[data]\nreference = "r1"\nversions = ["r1", "r2"]\n')
    code, env = run_cli(
        ["--config", str(cfgf), "rewrite", "--tree", str(tree), "--census", str(cj), "D_80100004"], capsys
    )
    assert code == 0 and env["result"]["expr"] == "D_80100000.b" and env["result"]["files"] == ["src/a.c"]
    text = (tree / "src" / "a.c").read_text()
    assert "D_80100000.b = 1;" in text and "extern s32 D_80100004" not in text
    with pytest.raises(DecompError):
        cli.parser().parse_args(["data", "gate"])  # --census is required


def test_gate_refuses_only_when_an_owner_form_exists(tmp_path: Path):
    cj = census_json(tmp_path)
    d = json.loads(cj.read_text())
    for c in d["clusters"]:
        for m in c["members"]:
            if m["sym"] == "D_80100004":
                m["path"], m["path_note"] = "D_80100000.pad[4]", "padding"
    cj.write_text(json.dumps(d))
    assert gate.check_diff(CFG, cj, "+++ b/src/b.c\n+extern s32 D_80100004;\n") == []
    assert gate.count_tree(CFG, cj, tmp_path / "tree") == 1  # still counted by the ratchet


def test_expand_units_adds_dependents_of_a_changed_header(tmp_path: Path):
    def run(cmd, **kw):
        assert cmd[0] == "grep" and "include/T.H" in cmd
        out = "build/r1/src/a.c.d\nbuild/r1/src/b.c.d\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    got = check.expand_units(tmp_path, "r1", ["include/T.H", "src/c.c", "README"], run)
    assert got == ["src/a.c", "src/b.c", "src/c.c"]
    assert check.expand_units(tmp_path, "r1", ["src/c.c"], run) == ["src/c.c"]


def _asm(tmp_path: Path, name: str, body: str) -> None:
    f = tmp_path / "ev/asm/r1/nonmatchings/span" / f"{name}.s"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)


def _ev_cfg(tmp_path: Path):
    return config.parse({"reference": "r1", "versions": ["r1", "r2"], "evidence_tree": str(tmp_path / "ev")})


def test_member_accepted_when_the_rom_gives_it_its_own_lui(tmp_path: Path):
    """F2b: a function that loads a cluster label with a lui of its own twice used a separate symbol in the source."""
    cj = census_json(tmp_path)
    diff = "+++ b/src/b.c\n+extern s32 D_80100004;\n"
    cfg = _ev_cfg(tmp_path)
    assert gate.check_diff(cfg, cj, diff)[0].startswith("src/b.c: MEMBER")  # no asm yet: unchanged behaviour
    _asm(tmp_path, "f", "lui $v0, %hi(D_80100004)\nlw $v0, %lo(D_80100004)($v0)\nlui $a0, %hi(D_80100000)\naddiu $a0, $a0, %lo(D_80100000)\n")
    assert gate.check_diff(cfg, cj, diff) == []
    assert gate.check_diff(CFG, cj, diff)[0].startswith("src/b.c: MEMBER")  # no evidence_tree configured: refuses


def test_member_refused_when_any_function_reaches_it_as_owner_plus_offset(tmp_path: Path):
    cj = census_json(tmp_path)
    diff = "+++ b/src/b.c\n+extern s32 D_80100004;\n"
    cfg = _ev_cfg(tmp_path)
    _asm(tmp_path, "f", "lui $v0, %hi(D_80100004)\nlw $v0, %lo(D_80100004)($v0)\nlui $a0, %hi(D_80100004)\nlw $a0, %lo(D_80100004)($a0)\n")
    assert gate.check_diff(cfg, cj, diff) == []
    _asm(tmp_path, "g", "lui $v1, %hi(D_80100000)\nlw $v0, %lo(D_80100004)($v1)\n")  # base register of the owner
    assert gate.check_diff(cfg, cj, diff)[0].startswith("src/b.c: MEMBER")


def test_single_own_lui_is_no_evidence(tmp_path: Path):
    cj = census_json(tmp_path)
    cfg = _ev_cfg(tmp_path)
    _asm(tmp_path, "f", "lui $v0, %hi(D_80100004)\nlw $v0, %lo(D_80100004)($v0)\nlw $a0, %lo(D_80100004)($v0)\n")
    assert gate.check_diff(cfg, cj, "+++ b/src/b.c\n+extern s32 D_80100004;\n")[0].startswith("src/b.c: MEMBER")


def test_stamp_dir_is_never_the_working_directory(tmp_path, monkeypatch):
    from abu_decomp.data import stamp

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DECOMP_DATA_STAMPS", raising=False)
    monkeypatch.delenv("DECOMP_DATA_CONFIG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert stamp.stamp_dir(None, None) == tmp_path / "state/decomp-data/stamps"
    monkeypatch.setenv("DECOMP_DATA_CONFIG", str(tmp_path / "cfg/rw.toml"))
    assert stamp.stamp_dir(None, None) == tmp_path / "cfg/stamps"
    assert stamp.stamp_dir(None, tmp_path / "other/x.toml") == tmp_path / "other/stamps"


def test_check_diff_already_committed_in_the_tree_is_no_refusal(tmp_path: Path):
    from abu_decomp.data import command

    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (tmp_path / "a.c").write_text("one\n")
    git("add", "a.c"), git("commit", "-qm", "base")
    (tmp_path / "a.c").write_text("two\n")
    diff = tmp_path.parent / "held.diff"
    diff.write_text(git("diff"))
    git("commit", "-qam", "change")  # the lane committed it: the diff no longer applies forward
    args = type("A", (), {"diff": diff, "write_baseline": False, "tree": tmp_path})()
    assert command._apply_diff(args) is False


def test_data_place_runs_unbake_sync_place_and_lists_rows_and_removed_junk(tmp_path: Path, monkeypatch):
    import argparse
    from types import SimpleNamespace

    from abu_decomp import tools
    from abu_decomp.data import command

    seen = []
    placed = [
        {"function": "F", "version": "us", "row": "literals_F = 0x800E1520;", "file": "versions/us/aliases.txt",
         "note": "", "removed": ["D_800E1530 (rom 0x2030)"]}
    ]  # fmt: skip

    def invoke(tool, profile, argv, **kw):
        seen.append((tool, list(argv), kw["project"]))
        doc = {"ok": True, "findings": [], "result": {"placed": placed, "written": ["versions/us/aliases.txt"]}}
        return SimpleNamespace(ok=True, document=doc)

    monkeypatch.setattr(tools, "invoke", invoke)
    monkeypatch.setattr(cli, "_profile", lambda p: "profile")
    args = argparse.Namespace(tree=tmp_path, check=True, function=["F", "G"], profile=None)
    result, code, findings = command._place(args)
    assert seen == [("unbake", ["sync", "--place", "F", "G", "--check"], tmp_path)]
    assert code == 0 and findings == []
    assert result["lines"] == [
        "F us: literals_F = 0x800E1520; (versions/us/aliases.txt); removed junk rows D_800E1530 (rom 0x2030)"
    ]


def test_data_place_carries_the_refusal_of_the_tool(tmp_path: Path, monkeypatch):
    import argparse
    from types import SimpleNamespace

    from abu_decomp import tools
    from abu_decomp.data import command

    doc = {"ok": False, "findings": [{"key": "place.gap", "blocking": True, "message": "us: F: its data spans"}]}
    monkeypatch.setattr(tools, "invoke", lambda *a, **k: SimpleNamespace(ok=False, document=doc))
    monkeypatch.setattr(cli, "_profile", lambda p: "profile")
    _, code, findings = command._place(argparse.Namespace(tree=tmp_path, check=False, function=["F"], profile=None))
    assert code == 1 and findings == [{"key": "place.gap", "blocking": True, "message": "us: F: its data spans"}]


def test_a_check_in_a_tree_holding_another_diff_stamps_by_building_alone(tmp_path, monkeypatch):
    """`data check --diff X` in a tree that also holds a second diff's edits built X alone on a pool slot (it used to refuse)."""
    import argparse
    import subprocess
    from types import SimpleNamespace

    from abu_decomp import cli
    from abu_decomp.data import command, stampbuild

    pool = tmp_path / "wt" / "POOL-SRC"
    pool.mkdir(parents=True)
    tree = tmp_path / "wt" / "L1"
    tree.mkdir()
    diff = tmp_path / "x.diff"
    diff.write_text("diff --git a/include/a.h b/include/a.h\n--- a/include/a.h\n+++ b/include/a.h\n@@ -1 +1 @@\n-x\n+y\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="include/a.h\ninclude/other.h\n", returncode=0))
    monkeypatch.setattr(cli, "_profile", lambda p: "profile")
    seen = {}

    def fake_build(cfg, profile, shared, d, stamps, at=None):
        seen["shared"] = shared
        return {"stamp": "stamps/ok"}, 0, []

    monkeypatch.setattr(stampbuild, "build", fake_build)
    args = argparse.Namespace(version=None, unit=None, diff=diff, tree=tree, since=None, stamps=tmp_path / "st", config=None, profile=None)
    assert command._stamp(args, None) == "stamps/ok" and seen["shared"] == pool
    monkeypatch.setattr(stampbuild, "build", lambda *a, **k: ({}, 1, [{"message": "bad rom"}]))
    assert "built alone and failed: bad rom" in command._stamp(args, None)
    pool.rmdir()  # no pool to build in: the old refusal stays
    assert "still holds edits" in command._stamp(args, None)
