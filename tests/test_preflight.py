"""`decomp cleanup preflight`: refusals grouped by (tool, code), classified, written as JSON and TSV."""

import json

from abu_decomp import preflight
from abu_decomp.preflight import Refusal


def test_normal_replaces_paths_and_the_key() -> None:
    assert preflight.normal("type not visible from include/A/B.H") == "type not visible from <path>"
    assert preflight.normal("Foo has no read-only data", "Foo") == "<key> has no read-only data"


def test_classify_names_defects_residues_and_the_rest() -> None:
    assert preflight.classify("owners.units", "type not visible from <path>")[0] == "defect"
    assert preflight.classify("owners.units", "no header for <path>")[0] == "residue"
    assert preflight.classify("structure.map", "fault: exit 1: boom")[0] == "defect"
    assert preflight.classify("owners.units", "never seen before") == ("unclassified", "")


def test_groups_count_and_keep_three_examples() -> None:
    rows = [Refusal("data.place", "<key> has no read-only data", f"f{i}") for i in range(5)] + [
        Refusal("x", "odd", "k")
    ]
    got = preflight.groups(rows)
    assert [(g["tool"], g["count"], g["examples"]) for g in got] == [
        ("data.place", 5, ["f0", "f1", "f2"]),
        ("x", 1, ["k"]),
    ]
    assert got[1]["class"] == "unclassified"


def test_parsers_turn_documents_into_refusals() -> None:
    stats, found = preflight._auto(
        {"rows": 4, "refused": {"strings": {"banned": 3}}, "examples": {"strings banned": ["a -> s_a", "b -> s_b"]}}
    )
    assert stats["rows"] == 4
    assert [(r.code, r.key) for r in found] == [
        ("strings banned", "a"),
        ("strings banned", "b"),
        ("strings banned", ""),
    ]
    _, found = preflight._structure({"refused": [{"module": "src/a.CPP", "reason": "tree not built: src/b.CPP"}]})
    assert [(r.code, r.key) for r in found] == [("tree not built: <path>", "src/a.CPP")]
    _, found = preflight._place(
        {"findings": [{"key": "Fn", "message": "Fn has no read-only data"}], "result": {"lines": ["x"]}}
    )
    assert [(r.code, r.key) for r in found] == [("no read-only data", "Fn")]
    _, found = preflight._step_t(
        {
            "reasons": {"already the original name": 2, "Foo is already a name in the project": 1},
            "skipped": [
                {"why": "Foo is already a name in the project", "tag": "T", "subject": "s"},
                {
                    "why": "3 of 9 accesses of flags have a base of no known record type (a/b.CPP)",
                    "tag": "U",
                    "subject": "f",
                },
            ],
        }
    )
    assert [(r.code, r.key) for r in found] == [
        ("already the original name", ""),
        ("already the original name", ""),
        ("<name> is already a name in the project", "T.s"),
        ("accesses of a field have a base of no known record type", "U.f"),
    ]
    assert [preflight.classify(r.tool, r.code)[0] for r in found] == ["residue"] * 3 + ["defect"]
    _, found = preflight._place(
        {"findings": [{"message": "A B has no read-only data of its own to place in any version"}], "result": {}}
    )
    fault = "fault: FileNotFoundError(2, 'No such file') at modcheck.py:70: retry"
    assert preflight.classify("structure.map", fault)[0] == "defect"
    none = "no read-only data of its own to place in any version"
    assert [(r.code, r.key) for r in found] == [(none, "A"), (none, "B")]
    assert preflight.classify("data.place", found[0].code)[0] == "defect"
    assert preflight.normal("type not visible from a/b.H Foo, Bar") == "type not visible from <path>"


def test_document_counts_unclassified_and_write_makes_json_and_tsv(tmp_path) -> None:
    report = preflight.Report(
        refusals=[
            Refusal("owners.units", "no header for <path>", "A"),
            Refusal("t", "odd", "k"),
            Refusal("t", "odd", "j"),
        ]
    )
    doc = preflight.document("abc", "abc123", report)
    assert doc["refusals"] == 3 and doc["unclassified"] == 2
    paths = preflight.write(tmp_path, "abc", doc)
    assert json.loads(paths[0].read_text())["groups"][0]["tool"] == "owners.units"
    head, first = paths[1].read_text().splitlines()[:2]
    assert head.startswith("tool\tcode\tclass") and first.startswith("owners.units\tno header for <path>\tresidue")
