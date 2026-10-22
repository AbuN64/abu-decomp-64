"""Claims cite runs: every `decomp` run leaves a record (its invocation id and outcome) in the tool state dir, and a
text that states a result (a commit message, an inbox entry) must cite an id whose recorded outcome supports it.

State dir: `ABU_DECOMP_INVOCATIONS`, else `invocations/` beside the profile's `events.path` (never a game repo).
Config (profile section `land`, key `claims`):
    "words":    {"fixed": "pass", "correct": "behaves", "benign": "benign", ...}   word -> what a cited run must show
                (`pass`: the run's check passed; `behaves`: a behave run found the same behaviour, since a byte check
                never compiles a non-matching body; `benign`: behave found only a benign difference)
    "commands": ["behave", "land", "ship", "make"]          commands whose runs may be cited (default: any but claims)
No `claims` key means no claim is checked; the records are written regardless.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.config import Profile
from abu_decomp.events import events_path

ID = re.compile(r"\b[0-9a-f]{12,32}\b")
FUNCTION = re.compile(r"\bfunc_[0-9A-Fa-f]{8}\w*")
NOT_RECORDED = frozenset({"claims", "describe", "mcp"})
NEEDS = ("pass", "behaves", "benign")


def supports(run: dict[str, Any], need: str) -> bool:
    if need == "behaves":
        return run.get("command") == "behave" and bool(run.get("passed"))
    return bool(run.get("passed" if need == "pass" else "benign"))


def state_dir(profile: Profile | None) -> Path | None:
    forced = os.environ.get("ABU_DECOMP_INVOCATIONS")
    if forced:
        return Path(forced)
    if profile is None:
        return None
    try:
        return events_path(profile).parent / "invocations"
    except (DecompError, KeyError, TypeError):
        return None


def outcome(command: str, ok: bool, result: Any) -> dict[str, Any]:
    """What a later claim may rest on: `passed` (the run's check held), `benign` (behave: only benign differences)."""
    document = result if isinstance(result, dict) else {}
    if command == "behave":
        verdict = document.get("verdict")
        found = document.get("classification") or {}
        return {
            "passed": ok and verdict == "same",
            "benign": ok and verdict == "differs" and found.get("real") is False,
            "verdict": verdict,
            "function": document.get("function"),
        }
    return {"passed": ok, "benign": False}


def record(profile: Profile | None, invocation: str, command: str, ok: bool, result: Any, argv: list[str]) -> None:
    """Persist one run; never raises (a missing state dir only means nothing can be cited)."""
    folder = state_dir(profile)
    if folder is None or command in NOT_RECORDED:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        row = {"invocation": invocation, "command": command, "ok": ok, "time": time.time(), "argv": argv[:12]}
        (folder / f"{invocation}.json").write_text(json.dumps({**row, **outcome(command, ok, result)}))
    except OSError:
        return


def lookup(profile: Profile | None, token: str) -> dict[str, Any] | None:
    folder = state_dir(profile)
    if folder is None or not folder.is_dir():
        return None
    hits = sorted(folder.glob(f"{token}*.json"))
    if len(hits) != 1:
        return None
    try:
        return json.loads(hits[0].read_text())
    except (OSError, ValueError):
        return None


def settings(profile: Profile) -> dict[str, Any] | None:
    try:
        table = profile.section("land").get("claims")
    except DecompError:
        return None
    if not table:
        return None
    words = table.get("words")
    if not isinstance(words, dict) or not words or not all(isinstance(k, str) and v in NEEDS for k, v in words.items()):
        raise DecompError(
            "config.schema", f"{profile.source_file}:land.claims.words: expected {{word: {'|'.join(NEEDS)}}}"
        )
    return {"words": {k.lower(): v for k, v in words.items()}, "commands": table.get("commands") or []}


def check(profile: Profile, text: str, function: str | None = None) -> dict[str, Any]:
    """{"cited": [ids that resolved], "problems": [{"word", "line", "need", "why", "next"}]} for one text."""
    config = settings(profile)
    if config is None:
        return {"cited": [], "problems": []}
    cited: list[dict[str, Any]] = []
    for token in dict.fromkeys(ID.findall(text)):
        found = lookup(profile, token)
        if found and (not config["commands"] or found["command"] in config["commands"]):
            cited.append(found)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in sorted(config["words"], key=len, reverse=True)) + r")\b", re.I
    )
    guess = function or next(iter(FUNCTION.findall(text)), None)
    profile_arg = f"--profile {profile.source_file}"
    behave = ""
    problems: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        for hit in pattern.finditer(line):
            word = hit.group(1).lower()
            need = config["words"][word]
            if word in seen:
                continue
            seen.add(word)
            if any(supports(run, need) for run in cited):
                continue
            why = (
                f"states '{word}' without citing an invocation id whose recorded outcome is {need}"
                if not cited
                else f"states '{word}' but none of the cited runs ({', '.join(r['invocation'][:12] for r in cited)}) "
                f"is {need}"
            )
            if guess and not behave:  # the candidate is the function's own source file, found only when needed
                from abu_decomp import arity

                where = arity.definition_file(profile, guess)
                behave = f"decomp {profile_arg} behave --function {guess}" + (f" --candidate {where}" if where else "")
            # Only behave can show behaviour or a benign difference; a byte check suits `pass` with no function named.
            nxt = behave or (f"decomp {profile_arg} make check" if need == "pass" else f"decomp {profile_arg} owes")
            problems.append({"word": word, "line": number, "need": need, "why": why, "next": nxt})
    return {"cited": [run["invocation"] for run in cited], "problems": problems}


def refusal_text(problem: dict[str, Any]) -> str:
    return f'{problem["why"]}; run `{problem["next"]}` and cite its "invocation" id in the text'
