"""A tas stop, crash, hang or exit says what it means: the tool classifies it, never the reader.

An agent that read `boot-missing: func_8041CAD8_de` as a timing hang kept pressing buttons at a function that has no
C body. Every outcome that is not a clean end carries the same claim: `kind` (no C body, unmapped data word, host
fault, hang, exit), `blocking` (true unless proven otherwise), `cannot_help` (what does not move it), `owner` (the
INCLUDE_ASM site or definition as file:line), the arity note when the function's parameter lists disagree, and one
`next` command. Unknown is blocking.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, arity, evidence

_FUNCTION = re.compile(r"func_[0-9A-Fa-f]{8}\w*")
_POINTER = re.compile(r"(?:code-pointer-|D_)([0-9A-Fa-f]{8})$")
_FAULT = re.compile(r"host-fault-([0-9A-Fa-f]+)$")


def _project(profile: Any) -> Path | None:
    from abu_decomp import tools

    try:
        return tools.project_dir(profile)
    except (DecompError, OSError, ValueError, KeyError):
        return None


def _grep(project: Path | None, pattern: str, keep: re.Pattern[str]) -> str | None:
    """The first `file:line` under project/src and include whose line matches `keep`."""
    if project is None:
        return None
    dirs = [str(d) for d in (project / "src", project / "include") if d.is_dir()]
    if not dirs:
        return None
    try:
        out = subprocess.run(
            [
                "grep",
                "-rnwF",
                "--include=*.c",
                "--include=*.C",
                "--include=*.cpp",
                "--include=*.CPP",
                "--include=*.h",
                "--include=*.H",
                pattern,
                *dirs,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for row in sorted(out.splitlines()):
        file, _, rest = row.partition(":")
        number, _, text = rest.partition(":")
        if keep.search(text):
            return f"{Path(file).relative_to(project)}:{number}"
    return None


def include_asm_site(project: Path | None, name: str) -> str | None:
    return _grep(project, name, re.compile(rf"INCLUDE_ASM\s*\(.*\b{re.escape(name)}\b"))


def definition_site(project: Path | None, name: str) -> str | None:
    return _grep(project, name, re.compile(rf"^\s*[\w\s\*]+\b{re.escape(name)}\s*\([^;]*$"))


def nonmatching(project: Path | None, names: list[str]) -> str | None:
    """The first of `names` whose body sits under the NON_MATCHING guard: it owes a match."""
    for name in names:
        site = definition_site(project, name) if project else None
        if site and project:
            fn = evidence.parse((project / site.rpartition(":")[0]).read_text(errors="replace")).get(name)
            if fn is not None and fn.kind == evidence.NM:
                return name
    return None


def claim(profile: Any, outcome: dict[str, Any], stacks: list[str] | None = None) -> dict[str, Any]:
    """The outcome's self-classification (empty for a clean end). Never raises."""
    what = outcome.get("outcome")
    if what in (None, "ok"):
        return {}
    subject = str(outcome.get("subject") or "")
    source = getattr(profile, "source_file", "PROFILE")
    project = _project(profile)
    frames = arity.frame_names(stacks or [])
    out: dict[str, Any] = {"blocking": True}
    owed = nonmatching(project, [n for n in dict.fromkeys([subject, *frames]) if _FUNCTION.fullmatch(n)])
    if what == "hang":
        out.update(
            kind="hang",
            cannot_help="more input is useless unless the picture shows the game waiting for a button",
            next=f"decomp --profile {source} tas look",
        )
    elif _FAULT.match(subject):
        owner_fn = next(iter(frames), None)
        out.update(
            kind="host fault",
            cannot_help="input cannot pass this point: the fault repeats on every replay",
            next=f"decomp --profile {source} arity show {owner_fn}"
            if owner_fn
            else f"decomp --profile {source} tas play MOVIE",
        )
        if owner_fn:
            site = definition_site(project, owner_fn) or include_asm_site(project, owner_fn)
            if site:
                out["owner"] = f"{owner_fn} at {site}"
    elif _FUNCTION.fullmatch(subject):
        site = include_asm_site(project, subject)
        if site and owed != subject:
            out.update(
                kind="no C body",
                cannot_help="input cannot pass this point: the function exists only as INCLUDE_ASM",
                owner=f"{subject} at {site}",
                next=f"recipe work {subject}",
            )
        elif owed == subject:
            out.update(
                kind="non-matching body",
                cannot_help="input cannot pass this point: the guarded body does not behave like the original",
                owner=f"{subject} at {definition_site(project, subject)}",
                next=f"recipe work {subject}",
            )
        else:
            out.update(
                kind="unknown",
                cannot_help="input cannot pass this point until the cause is known",
                next=f"decomp --profile {source} arity show {subject}",
            )
            site = definition_site(project, subject)
            if site:
                out["owner"] = f"{subject} at {site}"
    elif _POINTER.search(subject):
        word = _POINTER.search(subject)[1].upper()  # type: ignore[index]
        out.update(
            kind="unmapped data word",
            cannot_help="input cannot pass this point: a stored code pointer has no native target",
            next=f"grep -rn {word} {project / 'src' if project else 'src'}",
        )
        site = _grep(project, f"D_{word}", re.compile(r"."))
        if site:
            out["owner"] = f"D_{word} at {site}"
    else:
        out.update(
            kind="unknown",
            cannot_help="unknown, so treat as blocking: do not keep pressing buttons",
            next=f"decomp --profile {source} tas look",
        )
    names = [n for n in dict.fromkeys([subject, *frames]) if _FUNCTION.fullmatch(n)]
    notes = arity.notes(profile, names) if names else []
    if notes:
        out["arity"] = notes
    if out["kind"] == "no C body" or owed:
        who = owed or subject
        out["owes"] = (
            f"{who} owes a match: its frame is "
            + ("an INCLUDE_ASM stand-in" if not owed else "a NON_MATCHING body")
            + f"; the match is the target (recipe work {who}); recipe says when its budget is spent and the best "
            "behaves-the-same body may land fenced"
        )
        if owed and out["kind"] == "host fault" and not any(n["function"] == owed for n in notes):
            out["next"] = f"recipe work {owed}"
    out["claim"] = (
        f"{out['kind']}: {subject or what}; blocking={str(out['blocking']).lower()}; {out['cannot_help']}"
        + (f"; owner {out['owner']}" if "owner" in out else "")
        + (f"; {out['owes']}" if "owes" in out else "")
        + f"; next: {out['next']}"
    )
    return out
