import json
import subprocess
import time
from pathlib import Path

from abu_decomp import cleanup, gates


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    return root


SECTION = {
    "gates": [
        {
            "id": "a",
            "phase": "P3",
            "name": "func_",
            "measure": {"kind": "placeholders", "paths": r"^src/", "classes": ["func"]},
        },
        {
            "id": "b",
            "phase": "P4",
            "name": "fakematch",
            "measure": {"kind": "regex", "pattern": "FAKEMATCH", "paths": r"^src/"},
        },
        {"id": "c", "phase": "P2", "name": "common", "measure": {"kind": "files", "paths": r"^include/COMMON/"}},
        {"id": "d", "phase": "P1", "name": "tags", "measure": {"kind": "tags", "paths": r"^(src|include)/"}},
        {"id": "e", "phase": "P2", "name": "unmapped", "measure": {"kind": "unmapped", "map": "docs/MODULES.tsv"}},
        {
            "id": "f",
            "phase": "P0",
            "name": "broken",
            "measure": {"kind": "length", "file": "/nonexistent/x.json", "key": "k"},
        },
    ]
}


def test_every_gate_answers_a_number_and_a_broken_one_says_why_without_stopping_the_others(tmp_path):
    root = _repo(
        tmp_path,
        {
            "src/a.c": "void func_80001234(void); /* FAKEMATCH */ struct S { int a; };\n",
            "include/b.h": "struct S { int a; };\n",
            "include/COMMON/X.H": "",
            "docs/MODULES.tsv": "src/a.c\tmod\n",
        },
    )
    section = {**SECTION, "scratch": str(tmp_path / "scratch")}
    done = gates.run(root, "HEAD", section)
    got = {g["id"]: g.get("value") for g in done["gates"]}
    assert got == {"a": 1, "b": 1, "c": 1, "d": 1, "e": 0, "f": None}
    assert done["failed"] == 1 and "FileNotFoundError" in done["gates"][-1]["error"]
    assert not list((tmp_path / "scratch").glob("gates-*"))  # the unpacked revision is removed


def test_misfiled_and_address_named_count_per_folder_and_survive_a_rename_that_keeps_the_old_map_paths(tmp_path):
    tsv = "unit\tmodule\n" + "".join(
        f"{u}\t{m}\n"
        for u, m in (
            ("src/game/MENU_2/800E0B40.CPP", "src/game/MENU/TRACK.CPP"),
            ("src/game/MENU_2/800E0B48.CPP", "src/game/MENU/TRACK.CPP"),
            ("src/game/OTHER/80400000.CPP", "src/game/OTHER/NAMED.CPP"),
            ("src/game/A/dup.CPP", "src/game/A/X.CPP"),
            ("src/game/B/dup.CPP", "src/game/B/Y.CPP"),
        )
    )
    files = {"docs/MODULES.tsv": tsv, "src/game/MENU_2/stray.CPP": "", "include/COMMON/X.H": ""}
    section = {
        "scratch": str(tmp_path / "scratch"),
        "gates": [
            {"id": "m", "phase": "P2", "name": "misfiled", "measure": {"kind": "misfiled", "map": "docs/MODULES.tsv"}},
            {"id": "u", "phase": "P2", "name": "unmapped", "measure": {"kind": "unmapped", "map": "docs/MODULES.tsv"}},
            {
                "id": "a",
                "phase": "P2",
                "name": "addr",
                "measure": {"kind": "files", "paths": r"^src/.*/[0-9A-F]{8}(_\d+)?\.cpp$"},
            },
        ],
    }
    before = _repo(
        tmp_path / "before",
        {**files, **{u: "" for u in ("src/game/MENU_2/800E0B40.CPP", "src/game/MENU_2/800E0B48.CPP")}}
        | {u: "" for u in ("src/game/OTHER/80400000.CPP", "src/game/A/dup.CPP", "src/game/B/dup.CPP")},
    )
    got = {g["id"]: g for g in gates.run(before, "HEAD", section)["gates"]}
    assert (got["m"]["value"], got["u"]["value"], got["a"]["value"]) == (
        5,
        1,
        3,
    )  # stray.CPP has no row; the five mapped units all sit beside, not in, their module folder
    assert got["m"]["detail"] == {
        "units": 5,
        "unmapped": 1,
        "folders": {"game/MENU_2": 2, "game/A": 1, "game/B": 1, "game/OTHER": 1},
    }
    assert got["a"]["detail"]["folders"] == {"game/MENU_2": 2, "game/OTHER": 1}
    after = _repo(
        tmp_path / "after",
        {**files, **{u: "" for u in ("src/game/MENU/TRACK/800E0B40.CPP", "src/game/MENU/TRACK/800E0B48.CPP")}}
        | {u: "" for u in ("src/game/OTHER/NAMED/80400000.CPP", "src/game/A/X/dup.CPP", "src/game/B/Y/dup.CPP")},
    )
    now = {g["id"]: g for g in gates.run(after, "HEAD", section)["gates"]}
    assert (now["m"]["value"], now["u"]["value"], now["a"]["value"]) == (0, 1, 3)  # names stay address names
    assert now["a"]["detail"]["folders"] == {"game/MENU/TRACK": 2, "game/OTHER/NAMED": 1}
    lines = gates.gate_lines(list(got.values()), 1)
    assert lines[0].split()[:3] == ["P2", "5", "misfiled"] and lines[1].strip() == "4 folders: game/MENU_2=2"
    assert "folders" not in lines[0]


def test_hourly_rows_and_train_rounds_come_from_the_events_submits_and_history(tmp_path):
    book = tmp_path / "names"
    book.mkdir()
    (book / "ledger.jsonl").write_text(
        json.dumps({"seq": 1, "lane": "L1"}) + "\n" + json.dumps({"seq": 2, "lane": "L2"}) + "\n"
    )
    (book / "events.jsonl").write_text(
        json.dumps({"event": "landed", "seqs": [1, 2], "ts": "2026-10-25T10:05:00-0700"})
        + "\n"
        + json.dumps({"event": "returned", "seqs": [2], "ts": "2026-10-25T11:05:00-0700"})
        + "\n"
    )
    (book / "submits.jsonl").write_text(
        json.dumps({"lane": "L1", "refused": 3, "ts": "2026-10-25T10:30:00-0700"}) + "\n"
    )
    got = cleanup.hourly(book, "2026-10-25", time.time())
    assert got["L1"]["2026-10-25T10"] == {"landed": 1, "returned": 0, "refused": 3}
    assert got["L2"]["2026-10-25T11"]["returned"] == 1 and got["L2"]["2026-10-25T10"]["landed"] == 1
    history = tmp_path / "history.jsonl"
    rows = [
        {"ts": "20261025-100000", "results": [{"status": "LANDED"}, {"status": "DROPPED"}]},
        {"ts": "20261025-101000", "results": [{"status": "LANDED"}]},
        {"ts": "20261025-103000", "dry": True, "results": [{"status": "DRYRUN-OK"}]},
    ]
    history.write_text("".join(json.dumps(r) + "\n" for r in rows))
    train = cleanup.train_rounds(history, "2026-10-25", time.time())
    assert train["rounds"] == 2 and train["status"] == {"LANDED": 2, "DROPPED": 1}
    assert train["inputs_per_round"] == {"median": 1.5, "max": 2} and train["gap_minutes"]["median"] == 10.0
    assert train["rounds_per_hour"] == {"2026-10-25T10": 2}


def test_marked_counts_functions_opened_and_not_closed_later(tmp_path):
    claims = tmp_path / "CLAIMS.md"
    claims.write_text("A OPEN x\nB OPEN y\nA CLOSED z\nC note\nD OPEN w\n")
    spec = {"kind": "marked", "file": str(claims), "open": "OPEN", "closed": "CLOSED"}
    value, detail = gates._marked(None, spec)  # type: ignore[arg-type]
    assert (value, detail) == (2, {"named": 3})


def test_roms_gates_share_one_pool_tree_synced_to_the_revision(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from abu_decomp import shipproof
    from abu_decomp.data import config as config_mod
    from abu_decomp.data import stampbuild, trial

    root = _repo(tmp_path, {"src/a.c": "int x;\n"})
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    pool = tmp_path / "pool"
    subprocess.run(["git", "clone", "-q", str(root), str(pool)], check=True)
    leased, linked = [], []

    @contextmanager
    def fake_at_rev(cfg, profile, shared, rev, wait):
        leased.append((shared, rev, wait))
        yield tmp_path / "STAMP-S0"

    class FakeBuilder:
        def __init__(self, cfg, tree):
            linked.append(tree)

        def rom_check(self):
            return 0, ""

    monkeypatch.setattr(config_mod, "load", lambda path, tree=None: object())
    monkeypatch.setattr(stampbuild, "at_rev", fake_at_rev)
    monkeypatch.setattr(trial, "Builder", FakeBuilder)
    monkeypatch.setattr(
        shipproof, "rom_report", lambda tree: {"us": {"ok": True}, "de": {"ok": False}, "eu": {"ok": True}}
    )
    gate = {"measure": {"kind": "roms", "pool": str(pool), "wait": 5}}
    section = {
        "scratch": str(tmp_path / "scratch"),
        "gates": [{"id": f"r{i}", "phase": "P", "name": "roms", **gate} for i in range(3)],
    }
    done = gates.run(root, "HEAD", section)
    assert [g["value"] for g in done["gates"]] == [1, 1, 1] and done["failed"] == 0
    detail = done["gates"][0]["detail"]
    assert (detail["versions"], detail["exact"]) == (3, ["eu", "us"]) and set(detail["seconds"]) == {"tree", "link"}
    assert leased == [(pool, sha, 5.0)] and linked == [tmp_path / "STAMP-S0"]  # one lease, one link, for all three


def test_ledger_refuses_a_day_that_is_not_yyyy_mm_dd(tmp_path):
    import pytest

    from abu_decomp import DecompError

    for bad in ("20261025", "2026-1-5", "2026-13-40", "yesterday"):
        with pytest.raises(DecompError, match="YYYY-MM-DD"):
            cleanup.ledger(tmp_path, day=bad)
    assert cleanup.check_day("2026-10-25") == "2026-10-25" and cleanup.check_day(None) is None
