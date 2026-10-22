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


def test_gate_refuses_a_body_that_moved_to_another_unit_and_passes_it_in_place(tmp_path: Path, monkeypatch):
    from abu_decomp import evidence_gate

    stub = 'INCLUDE_ASM("o", g);\n'
    body = "int g(int a) { return a; }\n"
    other = "int h(void) { return 0; }\n"
    base = {"a.c": stub, "b.c": other}

    def landing_for(new: dict[str, str]):
        tree = tmp_path / ("t" + "".join(sorted(new)) + str(len(new["a.c"])))
        tree.mkdir(parents=True)
        for name, text in new.items():
            (tree / name).write_text(text)
        return SimpleNamespace(
            tree=tree,
            extra={"base": "B"},
            findings=[],
            behave=lambda o: pytest.fail("behave"),
            git=lambda *a, ok=True: base[a[1].split(":")[1]] if a[0] == "show" else "",
            snapshot=lambda: "N",
            changed_files=lambda: sorted(new),
            profile=SimpleNamespace(source_file=Path("p.json")),
        )

    cfg = {"suffixes": (".c",), "guard": "NON_MATCHING", "stand_in": "INCLUDE_ASM", "headers": [], "roms": []}
    cfg["slow_seconds"] = 60
    moved = evidence_gate.Gate(landing_for({"a.c": "", "b.c": other + body}), cfg, lambda: "v").run()[0]
    assert [r["function"] for r in moved] == ["g"] and "moved from unit a.c to unit b.c" in moved[0]["why"]
    assert (
        "its body belongs in place of its INCLUDE_ASM line in a.c" in moved[0]["why"]
        and "diff" in moved[0]["reproduce"]
    )
    assert evidence_gate.Gate(landing_for({"a.c": body, "b.c": other}), cfg, lambda: "v").run()[0] == []


# provenance and leftover names ------------------------------------------------------------------------------------
import json  # noqa: E402

from abu_decomp import provenance  # noqa: E402

RULES = [
    {"id": "m2c-local", "pattern": r"\b(?:sp[0-9A-F]{2,}|temp_\w+|var_\w+)\b", "hint": "rename"},
    {"id": "offset-cast", "pattern": r"\*\s*\(\s*\w+\s*\*\s*\)\s*\(\s*\(\s*\w+\s*\*\s*\)\s*\w+\s*\+\s*0x[0-9A-Fa-f]+"},
]
CLEAN = "void f(int *p) {\n    int count;\n    count = *p;\n}\n"
DIRTY = "void f(int *p) {\n    int sp50;\n    sp50 = *p;\n    *(int *)((char *)p + 0x4A0) = sp50;\n}\n"
BASE = 'INCLUDE_ASM("o", f);\n'


def tries_file(tmp_path, *bodies):
    path = tmp_path / "tries.jsonl"
    path.write_text("".join(json.dumps({"item": "f", "session": "s", "time": 1, "text": b}) + "\n" for b in bodies))
    return provenance.recorded([path])


def test_parse_lines():
    fn = evidence.parse("\nint f(void)\n{\n  return 1;\n}\n")["f"]
    assert (fn.start, fn.end) == (2, 4)


def test_unrecorded_body_refused(tmp_path):
    tries = tries_file(tmp_path, "#include <x.h>\n" + CLEAN.replace("count", "n"))
    rows = provenance.unproven(evidence.parse(BASE), evidence.parse(CLEAN), tries)
    assert [r["function"] for r in rows] == ["f"] and rows[0]["next"] == "recipe try f"
    assert "not produced/proven by recipe try" in rows[0]["why"]


def test_recorded_body_passes_even_with_other_header_and_whitespace(tmp_path):
    tries = tries_file(tmp_path, "#include <x.h>\nstatic void f(int *p)\n{ int count; /* hi */ count = *p; }\n")
    assert provenance.unproven(evidence.parse(BASE), evidence.parse(CLEAN), tries) == []


def test_declaration_edit_and_byte_checked_plain_need_no_try(tmp_path):
    tries = tries_file(tmp_path)
    old = evidence.parse("#ifndef NON_MATCHING\nvoid f(int *p) { *p = 1; }\n#endif")
    assert provenance.unproven(old, evidence.parse("void f(s32 *p) { *p = 1; }"), tries) == []
    plain_old, plain_new = evidence.parse("void f(int *p) { *p = 1; }"), evidence.parse("void f(int *p) { *p = 2; }")
    assert provenance.unproven(plain_old, plain_new, tries) == []


def test_leftovers_refused_with_lines():
    rows = provenance.leftover_names(evidence.parse(BASE), evidence.parse(DIRTY), BASE, DIRTY, RULES)
    assert (
        "line 2 m2c-local `sp50`" in rows[0]["why"] and "offset-cast" in rows[0]["why"] and "rename" in rows[0]["why"]
    )


def test_old_leftovers_pass_new_ones_block():
    old = evidence.parse(DIRTY)
    assert provenance.leftover_names(old, old, DIRTY, DIRTY, RULES) == []
    edited = DIRTY.replace("= *p;", "= *p + 1;")
    assert provenance.leftover_names(old, evidence.parse(edited), DIRTY, edited, RULES) == []
    more = DIRTY.replace("int sp50;", "int sp50;\n    int temp_s0;")
    rows = provenance.leftover_names(old, evidence.parse(more), DIRTY, more, RULES)
    assert "temp_s0" in rows[0]["why"] and "sp50" not in rows[0]["why"]
    assert provenance.leftover_names(evidence.parse(BASE), evidence.parse(CLEAN), BASE, CLEAN, RULES) == []
