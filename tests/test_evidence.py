from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from abu_decomp import evidence
from abu_decomp.evidence import ASM, NM, PLAIN, Fn, Score

SOURCE = """
#include "x.h"
int plain(int a) { return a + 1; /* } */ }
#ifdef NON_MATCHING
/* guessed { */
void guess(int *p) {
    if (*p) { *p = 0; }
}
#else
INCLUDE_ASM("ovl", guess);
#endif
INCLUDE_ASM("ovl", stub);
#ifndef NON_MATCHING
int other(void) { return 2; }
#else
INCLUDE_ASM("ovl", other);
#endif
static int table[2] = { 1, 2 };
"""


def test_parse_rungs():
    got = evidence.parse(SOURCE)
    assert {n: f.kind for n, f in got.items()} == {"plain": PLAIN, "guess": NM, "stub": ASM, "other": PLAIN}


def test_parse_ignores_comments_and_strings_in_text():
    a = evidence.parse("int f(void) { return 1; }")["f"]
    b = evidence.parse("int f(void) {\n  /* note */ return 1; // more\n}")["f"]
    assert a == b


def test_parse_custom_markers():
    got = evidence.parse("#ifdef GUESS\nint f(void){return 0;}\n#endif\nSTUB(a, g);", "GUESS", "STUB")
    assert got["f"].kind == NM and got["g"].kind == ASM


def measure_table(table):
    return lambda side, name: table[(side, name)]


def fn(kind, text="x"):
    return Fn(kind, text)


@pytest.mark.parametrize(
    ("before", "after", "was", "now", "ok"),
    [
        (fn(NM, "a"), fn(NM, "b"), Score(3, 48, "h1"), Score(4, 48, "h2"), True),  # k rises
        (fn(NM, "a"), fn(NM, "b"), Score(3, 48, "h1"), Score(3, 48, "h2"), False),  # guess tweak, k flat
        (fn(NM, "a"), fn(NM, "b"), Score(3, 48, "h1"), Score(2, 48, "h2"), False),  # k falls
        (fn(NM, "a"), fn(NM, "b"), Score(3, 48, "h1"), Score(3, 48, "h1"), True),  # byte-neutral cleanup
        (fn(NM, "a"), fn(NM, "b"), Score(48, 48, "h1"), Score(48, 48, "h2"), True),  # full agreement kept
        (fn(NM, "a"), fn(NM, "b"), Score(0, 48, None), Score(0, 48, None), False),  # unproven to unproven
        (fn(NM, "a"), fn(NM, "b"), Score(0, 48, "h1"), Score(5, 48, "h2"), True),  # unproven to behaves
        (fn(ASM), fn(NM, "b"), None, Score(0, 48, None, "does not compile"), False),
        (fn(ASM), fn(NM, "b"), None, Score(7, 48, "h"), True),
        (None, fn(NM, "b"), None, Score(0, 0, None), False),
        (fn(PLAIN, "a"), fn(NM, "b"), None, Score(40, 48, "h"), False),  # matching down to non-matching
        (fn(NM, "a"), fn(PLAIN, "b"), Score(3, 48, "h"), None, True),  # matches: make check proves it
        (fn(ASM), fn(PLAIN, "b"), None, None, True),
        (fn(PLAIN, "a"), fn(ASM), None, None, False),
        (fn(NM, "a"), fn(ASM), Score(3, 48, "h"), None, False),  # evidence thrown away
        (fn(NM, "a"), fn(ASM), Score(0, 48, None), None, True),  # unproven guess dropped
    ],
)
def test_judge(before, after, was, now, ok):
    base = {"f": before} if before else {}
    table = {("base", "f"): was, ("new", "f"): now}
    bad, good = evidence.judge(base, {"f": after}, measure_table(table))
    assert (not bad) is ok, (bad, good)
    if not ok:
        assert bad[0]["function"] == "f" and bad[0]["why"] and bad[0]["base"] and bad[0]["new"]


def test_untouched_function_is_not_measured():
    same = {"f": fn(NM, "a")}
    bad, good = evidence.judge(same, dict(same), lambda *_: pytest.fail("measured"))
    assert (bad, good) == ([], [])


def test_gate_reads_files_and_caches(tmp_path: Path, monkeypatch):
    from abu_decomp import evidence_gate

    old = '#ifdef NON_MATCHING\nint f(int a) { return a; }\n#else\nINCLUDE_ASM("o", f);\n#endif\n'
    new = old.replace("return a;", "return a + 1;")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.c").write_text(new)
    calls: list[Path] = []

    def behave(options):
        calls.append(options.candidate)
        k = 3
        return (
            {"counts": {"same": k}, "total": 48, "candidate": {"function_sha256": options.candidate.read_text()}},
            0,
            [],
        )

    def git(*args, ok=True):
        return old if args[0] == "show" else ""

    landing = SimpleNamespace(
        tree=tree,
        extra={"base": "B"},
        findings=[],
        behave=behave,
        git=git,
        snapshot=lambda: "N",
        changed_files=lambda: ["a.c"],
        profile=SimpleNamespace(source_file=Path("p.json")),
    )
    work = tmp_path / "work"
    import abu_decomp.behave.run as run_module

    monkeypatch.setattr(run_module, "configuration", lambda profile: {"work": str(work)})
    monkeypatch.setattr(run_module, "work_dir", lambda config: work)
    gate = evidence_gate.Gate(
        landing,
        {
            "suffixes": (".c",),
            "guard": "NON_MATCHING",
            "stand_in": "INCLUDE_ASM",
            "headers": ["include"],
            "roms": [],
            "slow_seconds": 60,
        },
        lambda: "v1",
    )
    refused, _ = gate.run()
    assert len(refused) == 1 and "reproduce" in refused[0] and "agreement did not rise" in refused[0]["why"]
    assert len(calls) == 2
    again = evidence_gate.Gate(landing, gate.config, lambda: "v1")
    again.run()
    assert len(calls) == 2 and all(m["cached"] for m in again.measures)


def test_landing_gate_turns_a_refusal_into_a_blocking_finding(monkeypatch):
    from abu_decomp import evidence_gate, land

    row = {
        "function": "f",
        "base": "behaves 3/48",
        "new": "C unproven",
        "why": "no behave evidence",
        "reproduce": "decomp behave",
    }
    monkeypatch.setattr(evidence_gate.Gate, "run", lambda self: ([row], []))
    landing = land.Landing.__new__(land.Landing)
    landing.config = {"evidence": {"slow_seconds": 60}}
    landing.clock, landing.gates, landing.findings = (lambda: 0.0), [], []
    landing.cpu_mark = 0.0
    assert landing.evidence() is False
    assert landing.findings[0]["key"] == "land.evidence" and "decomp behave" in landing.findings[0]["message"]
    assert landing.gates[0]["gate"] == "evidence" and landing.gates[0]["pass"] is False
