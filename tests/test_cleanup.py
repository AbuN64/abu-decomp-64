import subprocess

from abu_decomp import cleanup


def test_count_text_uses_the_one_pattern_set():
    got = cleanup.count_text("func_80001234 D_80001234 unk4 pad0C sp1C func_1234 Foo_unk spam D_8000123G")
    assert dict(got) == {"func": 1, "data": 1, "unk": 1, "pad": 1, "sp": 1, "var": 0, "arg": 0}


def test_decompiler_locals_and_unnamed_parameters_are_counted():
    got = cleanup.count_text("var_s0 = temp_f2 + arg0; var_a1; argument arg12 arg; x_var_s0 temp_")
    assert got["var"] == 3 and got["arg"] == 2


def test_evidence_asks_once_for_every_folder_and_reads_each_step_a_folder_has_classes_for():
    asked = []

    def ask(steps, targets):
        asked.append((steps, targets))
        return {
            "folders": {
                "src/game/A": {"L": {"namable": 9, "no_evidence": 4}, "N4": {"namable": 3, "no_evidence": 1}},
                "src/game/B": {"N5": {"namable": 0, "no_evidence": 0}, "L": {"namable": 7, "no_evidence": 0}},
            }
        }

    folders = {
        "game/A": {"sp": 2, "var": 0, "arg": 0, "data": 1, "func": 0},
        "include/COMMON": {"sp": 5},
        "game/B": {"func": 4},
    }
    got = cleanup.evidence(folders, ask)
    assert got["game/A"] == {"locals": {"namable": 9, "no_evidence": 4}, "data": {"namable": 3, "no_evidence": 1}}
    assert got["include/COMMON"] == {} and got["game/B"] == {"functions": {"namable": 0, "no_evidence": 0}}
    assert asked == [(["L", "N4", "N5"], ["src/game/A", "src/game/B"])]  # one recipe run, not one per folder and step
    assert cleanup.evidence({"include/X": {"sp": 1}}, ask) == {"include/X": {}} and len(asked) == 1


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


def test_count_rev_reads_a_commit_without_a_checkout(tmp_path):
    """CLEAN-PLAN-O1: counting a commit needed a checkout; --rev reads the commit's blobs."""

    def git(*a):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(tmp_path), *a],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "src/game/A").mkdir(parents=True)
    (tmp_path / "src/game/A/x.c").write_text("func_80000001 func_80000002")
    git("add", "-A")
    git("commit", "-qm", "one")
    (tmp_path / "src/game/A/x.c").write_text("Actor_Spawn func_80000002")
    git("commit", "-qam", "two")
    assert cleanup.count(tmp_path, rev="HEAD~1")["total"] == 2 and cleanup.count(tmp_path, rev="HEAD")["total"] == 1
    assert cleanup.count(tmp_path, "A", rev="HEAD~1")["folders"]["game/A"]["files"] == 1
    try:
        cleanup.count(tmp_path, rev="nosuch")
    except ValueError as e:
        assert "nosuch" in str(e)
    else:
        raise AssertionError("an unknown revision counted")


def test_slices_argv_hands_the_lane_count_map_and_assignments_to_the_recipe_slicer():
    assert cleanup.slices_argv(None, None, [], None) == ["name", "slices"]
    assert cleanup.slices_argv(12, "MODULES.tsv", ["a=S001,S002", "b=T01"], "/n") == [
        "name",
        "slices",
        "--lanes",
        "12",
        "--map",
        "MODULES.tsv",
        "--assign",
        "a=S001,S002",
        "--assign",
        "b=T01",
        "--out",
        "/n",
    ]
    doc = {"summary": ["98 slices"], "path": "/n/slices.json", "lane_files": ["/n/slices/a.json"]}
    assert cleanup.slices_lines(doc) == ["98 slices", "slices /n/slices.json", "/n/slices/a.json"]


def test_cleanup_slices_runs_recipe_in_the_project_and_refuses_when_recipe_does(monkeypatch, capsys, tmp_path):
    import argparse

    from abu_decomp import DecompError, cli, tools

    seen = []
    result = argparse.Namespace(ok=True, document={"ok": True, "summary": ["1 slices"], "path": "p", "lane_files": []})
    monkeypatch.setattr(cli, "_profile", lambda name: "PROFILE")
    monkeypatch.setattr(cli, "_own_tree", lambda args, verb: None)
    monkeypatch.setattr(tools, "project_dir", lambda profile, tree: tmp_path)
    monkeypatch.setattr(
        tools, "invoke", lambda tool, profile, argv, project=None: seen.append((tool, argv, project)) or result
    )
    args = argparse.Namespace(
        verb="slices",
        profile=None,
        lanes=8,
        map=None,
        assign=["x=S001"],
        names=None,
        dir=None,
        rev=None,
        evidence=False,
    )
    document, code, rows = cli._run_cleanup(args)
    assert seen == [("recipe", ["name", "slices", "--lanes", "8", "--assign", "x=S001"], tmp_path)]
    assert document["summary"] == ["1 slices"] and code == 0 and rows == []
    assert "1 slices" in capsys.readouterr().err
    result.ok, result.document = False, {"ok": False, "findings": [{"message": "no evidence"}]}
    try:
        cli._run_cleanup(args)
    except DecompError as e:
        assert "no evidence" in str(e)
    else:
        raise AssertionError("a refused slicer returned")


def test_distinct_counts_names_not_occurrences_and_scopes_slots_per_file(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    for path, text in {
        "src/game/A/x.c": "func_80000001 func_80000001 D_80000003 sp1C sp1C",
        "src/game/A/y.c": "func_80000001 sp1C arg0",
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    git("add", "-A")
    got = cleanup.count(tmp_path, distinct=True)
    assert got["classes"]["func"] == 3 and got["distinct"]["classes"]["func"] == 1  # one function, three spellings
    assert got["distinct"]["classes"]["sp"] == 2 and got["distinct"]["classes"]["arg"] == 1  # sp1C once per file
    assert got["distinct"]["total"] == 1 + 1 + 2 + 1 and got["folders"]["game/A"]["distinct"] == 5
    assert "distinct" not in cleanup.count(tmp_path)


def test_ledger_report_counts_rows_lanes_batches_and_slice_progress(tmp_path):
    import json
    import time

    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 600))
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 7200))
    (tmp_path / "slices").mkdir()
    (tmp_path / "slices/L1.json").write_text(
        json.dumps({"slice": "S1", "fns": ["fn:us-rev1:80000001", "fn:us-rev1:80000002", "fn:us-rev1:80000003"]})
    )
    rows = [{"seq": i, "ts": ts, "lane": "L1", "key": f"fn:us-rev1:8000000{i}"} for i in (1, 2, 3)]
    (tmp_path / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    events = [
        {"event": "made", "batch": 1, "seqs": [1, 2]},
        {"event": "landed", "batch": 1, "seqs": [1, 2]},
        {"event": "returned", "batch": 2, "seqs": [3]},
    ]
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    subs = [
        {"ts": ts, "lane": "L1", "rows": 5, "accepted": 3, "refused": 2, "by_rule": {"evidence": 2}},
        {"ts": old, "lane": "L1", "rows": 9, "accepted": 9, "refused": 0, "by_rule": {}},
    ]
    (tmp_path / "submits.jsonl").write_text("".join(json.dumps(r) + "\n" for r in subs))
    got = cleanup.ledger(tmp_path, hours=1.0, now=now)
    assert got["rows"] == {"queued": 0, "inflight": 0, "landed": 2, "returned": 1}
    assert got["batches"] == {"made": 0, "landed": 1}
    assert got["lanes"]["L1"] == {
        "submitted": 5,
        "accepted": 3,
        "refused": 2,
        "by_rule": {"evidence": 2},
        "rows_per_hour": 3.0,
    }
    assert got["slices"]["L1"] == {"slice": "S1", "owned_fns": 3, "named_fns": 2}  # the returned row does not count
