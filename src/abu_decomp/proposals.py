"""Recipe proposals for a landed tree: names and structure suggestions, never applied.

`decomp land` runs them on the proof tree once every gate passed, and `decomp cycle` runs them after the commits that
landed. Both go through the pinned Recipe Maker CLI (`tools.invoke`) and use only its proposal verbs: a proposal is a
saved patch and a list of moves under the project's `.unbake/recipe`, so no tracked file changes and a lane's pending
patch cannot stop applying because a rename moved its file. Applying a proposal (`recipe apply`, `recipe name submit`)
is an explicit operator action and is refused here.

A step is `{"name": "structure", "args": ["structure", "--json", "--paths", "{files}"]}`; the argument `{files}`
expands to the landed source files (one argument each) and a step that names it is skipped when there are none.
The summary of a step keeps its numbers, its short strings and the size of each list, plus the first suggestions:

    {"structure": {"ok": true, "moves": 3, "files": 2, "head": ["..."]}, ...}
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tools
from abu_decomp.config import Profile

PROPOSING = {"structure": None, "style": None, "flags": None, "look": None, "status": None, "name": {"next", "banners"}}
SUFFIXES = (".c", ".h", ".cpp")
HEAD = 12


def check_steps(file: Path, key: str, value: Any) -> list[dict[str, Any]]:
    """Validated steps: each a name and a Recipe verb that only proposes."""
    if not isinstance(value, list) or not value:
        raise DecompError("config.schema", f"{file}:{key}: expected a nonempty list of steps")
    steps = []
    for index, step in enumerate(value):
        where = f"{file}:{key}[{index}]"
        args = step.get("args") if isinstance(step, dict) else None
        if not isinstance(step, dict) or step.keys() - {"name", "args"} or not isinstance(step.get("name"), str):
            raise DecompError("config.schema", f"{where}: expected {{name, args}}")
        if not isinstance(args, list) or not args or not all(isinstance(item, str) and item for item in args):
            raise DecompError("config.schema", f"{where}.args: expected a nonempty list of strings")
        if args[0] not in PROPOSING:
            raise DecompError(
                "config.schema", f"{where}: recipe {args[0]} is not a proposal verb (apply and submit write the tree)"
            )
        allowed = PROPOSING[args[0]]
        if allowed and (len(args) < 2 or args[1] not in allowed):
            raise DecompError("config.schema", f"{where}: recipe {args[0]} takes one of {', '.join(sorted(allowed))}")
        steps.append({"name": step["name"], "args": list(args)})
    return steps


def source_files(project: Path, files: Sequence[str], suffixes: Sequence[str] = SUFFIXES) -> list[str]:
    """The landed files that are sources and exist in the project, sorted, relative to it."""
    wanted = tuple(item.lower() for item in suffixes)
    return sorted({f for f in files if f.lower().endswith(wanted) and (project / f).is_file()})


def expand(args: Sequence[str], files: Sequence[str]) -> list[str] | None:
    if "{files}" not in args:
        return list(args)
    return [item for arg in args for item in (files if arg == "{files}" else [arg])] if files else None


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Numbers and short strings as they are, lists as their length, and the first entries of the first list."""
    summary: dict[str, Any] = {}
    head: list[str] = []
    for key, value in result.items():
        if isinstance(value, bool | int | float) or (
            isinstance(value, str) and 0 < len(value) <= 80 and "\n" not in value
        ):
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = len(value)
            if value and not head and key not in ("files", "findings"):
                head = [json.dumps(item, sort_keys=True)[:120] for item in value[:HEAD]]
    return {**summary, **({"head": head} if head else {})}


def run(
    profile: Profile,
    project: Path | None,
    steps: Sequence[dict[str, Any]],
    files: Sequence[str],
    *,
    invoke: Callable[..., Any] = tools.invoke,
) -> dict[str, Any]:
    """Each step's summary by name; a failed or skipped step says why and never raises.

    project is the tree to target; None means the profile's own project."""
    found: dict[str, Any] = {}
    for step in steps:
        argv = expand(step["args"], files)
        if argv is None:
            found[step["name"]] = {"ok": True, "skipped": "no landed source files"}
            continue
        try:
            called = invoke("recipe", profile, argv, **({"project": project} if project else {}))
        except DecompError as error:
            found[step["name"]] = {"ok": False, "error": error.detail[:300]}
            continue
        document = called.document
        if not called.ok:
            first = (document.get("findings") or [{}])[0]
            found[step["name"]] = {"ok": False, "error": str(first.get("message", f"exit {called.returncode}"))[:300]}
            continue
        # Recipe's --json is one flat object {"ok": true, ...fields}; an enveloped tool keeps them under "result"
        body = document.get("result")
        if not isinstance(body, dict):
            body = {k: v for k, v in document.items() if k not in ("ok", "findings", "invocation", "stages")}
        found[step["name"]] = {"ok": True, **summarize(body)}
    return found


def line(summary: dict[str, Any]) -> str:
    """One line for an event message: step name, then the counts of its summary."""
    parts = []
    for name, step in summary.items():
        if not step.get("ok"):
            parts.append(f"{name} failed: {step.get('error', '?')[:60]}")
        elif step.get("skipped"):
            parts.append(f"{name} skipped")
        else:
            counts = [
                f"{v} {k}" for k, v in step.items() if k != "ok" and isinstance(v, int) and not isinstance(v, bool)
            ]
            parts.append(f"{name} " + (", ".join(counts[:4]) or "nothing proposed"))
    return "recipe proposes " + "; ".join(parts)
