"""`decomp repin`: the pin of another repository's commit that a tree builds from, set in every place it is spelled.

A tree whose build fetches a source repository at one commit (the port's decomp source) spells that commit in several
files. The `land.trees.<tree>.pin` config names the source checkout and each file with a pattern whose one group is
the pin. `repin` sets all of them to one commit in one change (left in the work tree for `ship --worktree`); the
`pin` gate of a plain-tree ship reads them back: all equal, the checkout the build fetched sits at the pin, and a pin
behind the source's ref is a note whose `next` is the repin verb.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, nextcmd
from abu_decomp.config import Profile

Run = Callable[[Sequence[str], Path, "dict[str, str] | None", "float | None"], "tuple[int, str, str]"]
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class RepinError(DecompError):
    pass


def _repin(profile_file: Path | None, tree: Path, source: Path, ref: str = "HEAD") -> str:
    """The repin command (needs the seat profile), else the read-only command that shows the source's tip."""
    if profile_file:
        return f"decomp --profile {profile_file} repin --tree {tree}"
    return f"git -C {source} log -1 --oneline {ref}"


def commit(run: Run, repo: Path, ref: str) -> str:
    """The full commit `ref` names in `repo`; RepinError when it names none."""
    code, out, err = run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], repo, None, 60)
    sha = out.strip()
    if code or not HEX40.match(sha):
        raise RepinError("repin.commit", f"{ref} is not a commit of {repo}: {(err or out).strip()[-200:]}")
    return sha


def read(pin: dict[str, Any], tree: Path) -> dict[str, list[str]]:
    """file -> the pins its pattern finds (empty when the file or the pattern is missing)."""
    found: dict[str, list[str]] = {}
    for row in pin["files"]:
        path = tree / row["path"]
        text = path.read_text() if path.is_file() else ""
        found[row["path"]] = [m.group(1) for m in row["pattern"].finditer(text)]
    return found


def check(pin: dict[str, Any], tree: Path, run: Run, profile_file: Path | None = None) -> dict[str, Any]:
    """{"pin": the tree's pin or None, "findings": [...]}: the pin gate's reading of the tree as it is."""
    findings: list[dict[str, Any]] = []
    places = read(pin, tree)
    values = {v for rows in places.values() for v in rows}
    empty = sorted(path for path, rows in places.items() if not rows)
    if empty:
        findings.append(
            {"key": "pin.missing", "blocking": True, "message": f"no pin found in {', '.join(empty)}",
             "next": f"cat {tree / empty[0]}"}
        )  # fmt: skip
    if len(values) > 1:
        spelled = "; ".join(f"{p}={','.join(v[:9] for v in r)}" for p, r in places.items())
        findings.append(
            {"key": "pin.differ", "blocking": True,
             "message": "the pin places disagree: " + spelled,
             "next": _repin(profile_file, tree, pin["source"], pin["ref"])}
        )  # fmt: skip
    current = next(iter(values)) if len(values) == 1 else None
    if current and pin["fetched"]:
        code, out, _ = run(["git", "-C", str(pin["fetched"]), "rev-parse", "HEAD"], tree, None, 60)
        if code or out.strip() != current:
            findings.append(
                {"key": "pin.fetched", "blocking": True,
                 "message": f"the fetched source {pin['fetched']} is at {out.strip()[:12] or 'nothing'}, "
                 f"the pin is {current[:12]}",
                 "next": _repin(profile_file, tree, pin["fetched"])}
            )  # fmt: skip
    if current:
        try:
            head = commit(run, pin["source"], pin["ref"])
        except RepinError as error:
            findings.append({"key": "pin.source", "blocking": False, "message": str(error)})
        else:
            if head != current:
                findings.append(
                    {"key": "pin.behind", "blocking": False,
                     "message": f"the pin {current[:12]} lags {pin['source'].name} {pin['ref']} at {head[:12]}",
                     "next": _repin(profile_file, tree, pin["source"], pin["ref"])}
                )  # fmt: skip
    return {"pin": current, "findings": findings}


def apply(pin: dict[str, Any], tree: Path, sha: str) -> list[str]:
    """Set every pin place to `sha`; the files changed. A place whose pattern finds nothing is an error (no write)."""
    edits: list[tuple[Path, str]] = []
    for row in pin["files"]:
        path = tree / row["path"]
        text = path.read_text() if path.is_file() else ""
        if not row["pattern"].search(text):
            raise RepinError(
                "repin.pattern", f"{row['path']} holds no pin the pattern {row['pattern'].pattern!r} reads"
            )
        edits.append((path, row["pattern"].sub(lambda m: m.group(0).replace(m.group(1), sha), text)))
    changed = []
    for path, text in edits:
        if path.read_text() != text:
            path.write_text(text)
            changed.append(str(path.relative_to(tree)))
    return changed


def lag(profile: Profile, path: Path, run: Run | None = None) -> dict[str, str] | None:
    """{tree, pin, head, next} when `path` lies in a tree whose pin trails its source's ref (its build cannot
    configure against the moved source until `decomp repin` runs); None when it has no pin or the pin is current."""
    from abu_decomp import land

    run = run or land.run_process
    path = path.expanduser().resolve()
    for key in profile.section("land").get("trees", {}):
        tree = profile.path(f"land.trees.{key}", key)
        if path != tree and tree not in path.parents:
            continue
        pin = land.settings(profile, tree)["pin"]
        if pin is None:
            return None
        found = check(pin, tree, run, profile.source_file)
        behind = next((f for f in found["findings"] if f["key"] == "pin.behind"), None)
        if behind is None:
            return None
        head = commit(run, pin["source"], pin["ref"])
        return {
            "tree": str(tree),
            "pin": found["pin"] or "",
            "head": head,
            "next": f"decomp --profile {profile.source_file} repin --tree {tree}",
        }
    return None


def checkout_fetched(run: Run, pin: dict[str, Any], sha: str) -> str | None:
    """Put the checkout the build fetched (a separate clone from the source, as the page port's FetchContent dir is)
    on `sha`: fetch the commit from the source, detach there. None when it is the source itself or absent."""
    fetched = pin["fetched"]
    if fetched is None or fetched == pin["source"] or not (fetched / ".git").exists():
        return None
    for argv in (
        ["git", "-C", str(fetched), "fetch", "--no-tags", str(pin["source"]), sha],
        ["git", "-C", str(fetched), "checkout", "--detach", "--quiet", sha],
    ):
        code, out, err = run(argv, fetched, None, 300)
        if code:
            raise RepinError("repin.fetched", f"{argv[3]} failed in {fetched}: {(err or out).strip()[-200:]}")
    return str(fetched)


def rebuild_page(profile: Profile, run: Run, tree: Path) -> dict[str, Any] | None:
    """The page port's incremental rebuild (`tas.build.rebuild`) when no replay is live; {"skipped": why} otherwise.
    None when the profile has no tas build command. A running replay holds the binary, so it is never rebuilt under
    one."""
    from abu_decomp import tas
    from abu_decomp.config import ConfigError

    try:
        cfg = tas.settings(profile)
    except ConfigError:
        return None
    command = cfg["build"].get("rebuild")
    if not command:
        return None
    live = [row["id"] for _, row in tas.run_rows(cfg) if row.get("status") == "running"]
    if live:
        return {"skipped": f"replay live ({', '.join(live[:3])}); rerun this verb when it ends", "command": command}
    code, out, err = run(["nice", "-n", "10", *shlex.split(command)], tree, None, 900)
    result: dict[str, Any] = {"command": command, "exit": code}
    if code:
        result["tail"] = (err or out).strip()[-400:]
    return result


def repin(
    profile: Profile, tree: Path, to: str | None, run: Run | None = None, rebuild: bool = True
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    from abu_decomp import land

    run = run or land.run_process
    tree = tree.expanduser().resolve()
    pin = land.settings(profile, tree)["pin"]
    if pin is None:
        raise RepinError("repin.config", f"land.trees has no pin for {tree}")
    sha = commit(run, pin["source"], to or pin["ref"])
    before = check(pin, tree, run)["pin"]
    changed = apply(pin, tree, sha)
    fetched = checkout_fetched(run, pin, sha)
    built = rebuild_page(profile, run, tree) if rebuild else None
    after = check(pin, tree, run)
    blocking = [
        f for f in after["findings"] if f["blocking"] and f["key"] != "pin.fetched"
    ]  # fetched follows the build
    _, moved, _ = run(["git", "-C", str(pin["source"]), "log", "-1", "--format=%s", sha], tree, None, 60)
    head = f"Repin to {pin['source'].name} {sha[:7]}"
    limit = land.settings(profile, tree)["subject_limit"]
    ship = nextcmd.ship_command(
        profile.source_file, tree, "repin", f"{head}: {moved.strip()}", worktree=True, limit=limit
    )
    result = {"tree": str(tree), "from": before, "to": sha, "changed": changed, "next": ship}
    if fetched:
        result["fetched"] = fetched
    if built is not None:
        result["page_build"] = built
    if built and built.get("exit"):
        blocking.append(
            {"key": "repin.build", "blocking": True, "message": f"page port rebuild failed: {built['tail']}"}
        )
    return result, int(bool(blocking)), blocking
