"""`decomp tools deploy`: install one decomp commit and its tools into venvs, after the pins agree.

The tools are installed from git commits only (`name @ git+URL@SHA`, a full 40-hex SHA; a wheel or a branch is
refused), so a venv's provenance is always a commit that `decomp dependencies` can check. Before anything is
installed the pins that must agree are compared and the ones that would have to move are printed:

  decomp's pyproject pins of unbake, Recipe Maker and Cake Taste (at DECOMP_SHA) against the commits asked for;
  Recipe Maker's own pin of unbake (at its commit) against the unbake commit.

A game project pins none of our tools (it builds with its own make), so a deploy never asks a project to move. It
declares the Python packages its make runs (splat, spimdisasm, ...) in `requirements.txt` at its root, each pinned
`name==version`; the deploy installs them into every venv beside the decomp commit and its `dev` extra, so a plain
deploy gives a working venv with no hand reinstall. The file is read from the project repository at one revision,
never from a work tree (a stale or detached tree cannot drop packages): `--project-sha`, else the project's main (the
profile's `land.remote` / `land.branch`, default origin/main) fetched at deploy time. The smoke test imports every
declared package from the new venv, so a venv that cannot import one is a failed deploy.

Any mismatch refuses the deploy and names the file, the pin, what it says now and what it must say. When they agree,
each checkout the seat names in `tools.checkouts` (a tool's code run from a git checkout) is brought to the pinned
commit (fetch, detached checkout) and refused, untouched, when it has local changes.

A deploy never changes a file under a running process. Each `--venv PATH` is a live path that is a symlink: the four
requirements are installed into a fresh venv beside it (`PATH.<sha12>`, pip run by that venv's own python so console
script shebangs point into it), the smoke test (`decomp dependencies` for provenance and `unbake symbols --version V`
on the project) runs against the fresh venv, and only then is the symlink swapped with rename(2). Processes started
through PATH's console scripts keep their versioned tree; superseded versions stay until purge removes them (policy
`venv_versions`: not the live target, not the newest rollback, no process using it). A live path that is still a real
directory is refused (a deploy only swaps symlinks), and the link is read back.

The new tool's Makefile and local.mk are one contract with the trees that run them (a retired recipe variable or
cc_cache.py mode in a tree's files fails every build there, 10-27), so a deploy ends by regenerating both in every
work tree of the project (`git worktree list`) with the new venv's `unbake sync --makefile`. A tree some live process
runs in is skipped and named; a tree whose own content refuses the sync is named with the refusal. Neither fails the
deploy: `resync` in the result lists every tree with its outcome.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tools
from abu_decomp.config import ConfigError, Profile

DECOMP_URL = "https://github.com/AbuN64/abu-decomp-64"
SHA = re.compile(r"[0-9a-f]{40}")
_EXACT = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.!+_*-]+$")
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*@\s*git\+(?P<url>https://[^@\s]+)@(?P<commit>[0-9a-f]{40})$")


Fetch = Callable[[str, str, str], str]
Requirements = Callable[[Path, str, str, str | None], "tuple[str, str]"]
Run = Callable[[Sequence[str], Path | None], "tuple[int, str]"]


class DeployError(DecompError):
    pass


def fetch_file(url: str, sha: str, path: str) -> str:
    """File `path` of `url` at the commit `sha`, fetched shallow into a scratch directory."""
    with tempfile.TemporaryDirectory(prefix="deploy-") as scratch:
        for argv in (["git", "init", "-q", scratch], ["git", "-C", scratch, "fetch", "-q", "--depth=1", url, sha]):
            done = subprocess.run(argv, capture_output=True, text=True, check=False)
            if done.returncode:
                raise DeployError("deploy.fetch", f"cannot fetch {url} at {sha}: {done.stderr.strip()[-300:]}")
        shown = subprocess.run(
            ["git", "-C", scratch, "show", f"FETCH_HEAD:{path}"], capture_output=True, text=True, check=False
        )
        if shown.returncode:
            raise DeployError("deploy.fetch", f"{url} at {sha} has no {path}")
        return shown.stdout


def requirements_at(project: Path, remote: str, branch: str, sha: str | None) -> tuple[str, str]:
    """(commit, text of `requirements.txt` there) from the project repository: `sha` when given, else the tip of
    `remote`/`branch` fetched now. The text is empty when that commit has no such file."""

    def git(*argv: str) -> str:
        done = subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True, check=False)
        if done.returncode:
            raise DeployError(
                "deploy.requirements", f"{project}: git {' '.join(argv)} failed: {done.stderr.strip()[-300:]}"
            )
        return done.stdout

    if sha is None:
        git("fetch", "-q", remote, branch)
    commit = git("rev-parse", "--verify", f"{sha or 'FETCH_HEAD'}^{{commit}}").strip()
    listed = git("ls-tree", "--name-only", commit, "requirements.txt")
    return commit, git("show", f"{commit}:requirements.txt") if listed.strip() else ""


def run_command(argv: Sequence[str], cwd: Path | None = None) -> tuple[int, str]:
    done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr)[-2000:]


def pins_of(pyproject: str) -> dict[str, dict[str, str]]:
    """Normalised distribution name -> {name, url, commit} for each direct git pin in `dependencies`."""
    found: dict[str, dict[str, str]] = {}
    for row in tomllib.loads(pyproject).get("project", {}).get("dependencies", []):
        match = _PIN.match(row.strip())
        if match:
            found[tools._normal(match["name"])] = {k: match[k] for k in ("name", "url", "commit")}
    return found


def check_sha(label: str, value: str) -> str:
    if not SHA.fullmatch(value):
        raise DeployError(
            "deploy.sha", f"{label} must be a full 40-hex commit SHA (git+URL@SHA installs only), got {value!r}"
        )
    return value


def plan(decomp: str, wanted: dict[str, str], fetch: Fetch) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(requirements to install, pins that must move). `wanted` maps tool command to the commit asked for."""
    own = pins_of(fetch(DECOMP_URL, decomp, "pyproject.toml"))
    moves: list[dict[str, str]] = []
    requirements = [{"name": tools.DISTRIBUTION, "url": DECOMP_URL, "commit": decomp}]
    tool_pyprojects: dict[str, str] = {}
    for command, tool in tools.TOOLS.items():
        pin = own.get(tools._normal(tool.distribution))
        if pin is None:
            raise DeployError("deploy.pin", f"decomp {decomp[:12]} declares no commit pin for {tool.distribution}")
        commit = wanted.get(command) or pin["commit"]
        if commit != pin["commit"]:
            moves.append(
                {
                    "file": "abu-decomp-64 pyproject.toml",
                    "pin": pin["name"],
                    "now": pin["commit"],
                    "must": commit,
                    "why": f"{command} is deployed at {commit[:12]}",
                }
            )
        requirements.append({"name": pin["name"], "url": pin["url"], "commit": commit})
        tool_pyprojects[command] = fetch(pin["url"], commit, "pyproject.toml")
    unbake_commit = next(r["commit"] for r in requirements if tools._normal(r["name"]) == "unbake")
    recipe_pin = pins_of(tool_pyprojects["recipe"]).get("unbake")
    if recipe_pin is None or recipe_pin["commit"] != unbake_commit:
        moves.append(
            {
                "file": "recipe-maker-64 pyproject.toml",
                "pin": "unbake",
                "now": recipe_pin["commit"] if recipe_pin else "none",
                "must": unbake_commit,
                "why": "Recipe Maker must pin the deployed unbake, then decomp's Recipe pin moves to that commit",
            }
        )
    return requirements, moves


def versioned_path(live: Path, decomp: str) -> Path:
    """`<live>.<sha12>`, or `<live>.<sha12>-N` when that name is already taken (a redeploy of the same commit)."""
    base = live.with_name(f"{live.name}.{decomp[:12]}")
    candidate, number = base, 1
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = base.with_name(f"{base.name}-{number}")
    return candidate


def point(live: Path, target: Path) -> dict[str, Any]:
    """Make `live` a symlink to `target`, atomically: a fresh link is renamed over it (rename(2))."""
    fresh = live.with_name(f".{live.name}.{os.getpid()}.new")
    fresh.unlink(missing_ok=True)
    fresh.symlink_to(target)
    row: dict[str, Any] = {"live": str(live), "target": str(target)}
    try:
        row["previous"] = os.path.realpath(live) if live.exists() else ""
        os.rename(fresh, live)
    finally:
        fresh.unlink(missing_ok=True)
    row["readback"] = os.path.realpath(live) == os.path.realpath(target) and live.is_symlink()
    return row


def project_requirements(text: str, source: str) -> list[str]:
    """The exact pins of the project's `requirements.txt` (`text`, read from `source`); a line that is not
    `name==version` is refused, so a venv never holds a package the project did not pin."""
    rows = [row.split("#")[0].strip() for row in text.splitlines()]
    rows = [row for row in rows if row]
    for row in rows:
        if not _EXACT.match(row):
            raise DeployError("deploy.requirements", f"{source}: {row!r} is not an exact name==version pin")
    return rows


# Run by the new venv's python: import the module each declared distribution is named for (splat64 -> splat; its
# distribution may also ship stray top-level modules such as `dist`, which are not the package), or every top-level
# module of a distribution whose name matches none.
IMPORT_CHECK = """\
import importlib
import importlib.metadata as metadata
import re
import sys

def normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()

owners = metadata.packages_distributions()
for spec in sys.argv[1:]:
    dist = normal(re.split(r"[\\[=]", spec)[0])
    modules = sorted(mod for mod, names in owners.items() if dist in {normal(n) for n in names})
    if not modules:
        sys.exit(f"{spec}: installs no importable module")
    named = [mod for mod in modules if normal(mod) in (dist, dist.rstrip("0123456789"))]
    for module in named or modules:
        importlib.import_module(module)
    print(spec, "imports", *(named or modules))
"""


def install(
    venv: Path, base: Path, requirements: Sequence[dict[str, str]], run: Run, packages: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Create `venv` with the python of `base` (the venv it replaces), then pip install with the new venv's python:
    the tools by commit (decomp with its `dev` extra) and the project's pinned `packages`."""
    python = venv / "bin" / "python"
    specs = [
        f"{r['name']}{'[dev]' if tools._normal(r['name']) == tools._normal(tools.DISTRIBUTION) else ''}"
        f" @ git+{r['url']}@{r['commit']}"
        for r in requirements
    ]
    specs += packages
    done: list[dict[str, Any]] = []
    for name, argv in (
        ("create", [str(base / "bin" / "python"), "-m", "venv", str(venv)]),
        ("install", [str(python), "-m", "pip", "install", *specs]),
    ):
        code, output = run(argv, None)
        done.append({"step": name, "ok": code == 0, "tail": output[-400:] if code else ""})
        if code:
            break
    return done


def smoke(venv: Path, project: Path | None, host: Path, run: Run, packages: Sequence[str] = ()) -> list[dict[str, Any]]:
    """`decomp dependencies` (every installed tool at its pinned commit), an import of every declared project package,
    and `unbake symbols --version V` on the project, V being the first version directory of the project."""
    results = []
    code, output = run([str(venv / "bin" / "decomp"), "dependencies"], None)
    results.append({"check": "decomp dependencies", "ok": code == 0, "tail": output[-300:] if code else ""})
    if packages:
        code, output = run([str(venv / "bin" / "python"), "-c", IMPORT_CHECK, *packages], None)
        results.append(
            {"check": "import " + " ".join(packages), "ok": code == 0, "tail": output[-300:] if code else ""}
        )
    root = project / "versions" if project else None
    versions = sorted(p.name for p in root.iterdir() if p.is_dir()) if root and root.is_dir() else []
    if not project or not versions:
        results.append(
            {"check": "unbake symbols", "ok": False, "tail": "the project has no versions/ directory to ask"}
        )
        return results
    argv = [
        str(venv / "bin" / "unbake"),
        "--host",
        str(host),
        "--project",
        str(project),
        "symbols",
        "--version",
        versions[0],
    ]
    code, output = run(argv, project)
    results.append(
        {
            "check": f"unbake symbols --version {versions[0]}",
            "ok": code == 0 and bool(output.strip()),
            "tail": output[-300:] if code else "",
        }
    )
    return results


def checkout_steps(
    profile: Profile, requirements: Sequence[dict[str, str]]
) -> tuple[dict[str, tuple[Path, str]], list[dict[str, Any]]]:
    """The checkouts the seat runs tool code from (`tools.checkouts`) with the commit each must sit at, and a
    blocking finding for every one that has local changes (aligning would discard or carry them; never done)."""
    commit = {tools._normal(r["name"]): r["commit"] for r in requirements}
    wanted = {
        command: (path, commit[tools._normal(tools.TOOLS[command].distribution)])
        for command, path in tools.checkouts_of(profile).items()
    }
    findings = []
    for command, (path, _) in wanted.items():
        if not path.is_dir():
            findings.append(
                {"key": "deploy.checkout", "blocking": True, "message": f"{command} checkout {path} is not a directory"}
            )
        elif dirty := tools.checkout_dirty(path):
            findings.append(
                {
                    "key": "deploy.checkout_dirty",
                    "blocking": True,
                    "message": f"{command} checkout {path} has local changes and is left as it is: "
                    f"{'; '.join(dirty[:5])}",
                }
            )
    return wanted, findings


def align_checkouts(wanted: dict[str, tuple[Path, str]], run: Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch each checkout, then a detached checkout of its pinned commit. (rows done, findings)"""
    done: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for command, (path, sha) in wanted.items():
        code, output = run(["git", "-C", str(path), "fetch", "-q", "origin"], None)
        if code == 0:
            code, output = run(["git", "-C", str(path), "checkout", "-q", "--detach", sha], None)
        done.append({"checkout": command, "path": str(path), "commit": sha, "ok": code == 0})
        if code:
            findings.append(
                {
                    "key": "deploy.checkout",
                    "blocking": True,
                    "message": f"{command} checkout {path} cannot reach {sha}: {output[-300:]}",
                }
            )
    return done, findings


def worktrees(project: Path, run: Run) -> list[Path]:
    """The project's work trees (its own checkout among them) that carry a Makefile."""
    code, output = run(["git", "worktree", "list", "--porcelain"], project)
    trees = (
        [Path(line.split(" ", 1)[1]) for line in output.splitlines() if line.startswith("worktree ")]
        if not code
        else []
    )
    return [tree for tree in trees if (tree / "Makefile").is_file()]


def holders(tree: Path) -> list[int]:
    """The live processes whose working directory is inside `tree`."""
    root, mine, found = os.path.realpath(tree), os.getpid(), []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and int(entry.name) != mine:
            try:
                cwd = os.readlink(f"{entry.path}/cwd")
            except OSError:
                continue
            if cwd == root or cwd.startswith(root + os.sep):
                found.append(int(entry.name))
    return found


def resync(venv: Path, host: Path, project: Path, run: Run) -> list[dict[str, Any]]:
    """`unbake sync --makefile` (Makefile and local.mk) of the new venv in every work tree of the project."""
    rows = []
    for tree in worktrees(project, run):
        if pids := holders(tree):
            rows.append({"tree": str(tree), "outcome": "skipped", "held_by": pids[:5]})
            continue
        argv = [str(venv / "bin" / "unbake"), "--host", str(host), "--project", str(tree), "sync", "--makefile"]
        code, output = run(argv, tree)
        rows.append(
            {"tree": str(tree), "outcome": "refused" if code else "synced", "tail": output[-300:] if code else ""}
        )
    return rows


def _land_table(profile: Profile) -> dict[str, Any]:
    """The profile's `land` section (where the project's main lives), empty when the seat has none."""
    try:
        return profile.section("land")
    except ConfigError:
        return {}


def deploy(
    profile: Profile,
    decomp: str,
    venvs: Sequence[Path],
    commits: dict[str, str | None],
    *,
    project_sha: str | None = None,
    fetch: Fetch = fetch_file,
    run: Run = run_command,
    requirements_of: Requirements = requirements_at,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    check_sha("--sha", decomp)
    wanted = {command: check_sha(f"--{command}", sha) for command, sha in commits.items() if sha}
    if not venvs:
        raise DeployError("deploy.venv", "pass at least one --venv")
    for venv in venvs:
        if not (venv / "bin" / "python").is_file():
            raise DeployError("deploy.venv", f"{venv} is not a venv (no bin/python)")
        if not venv.is_symlink():
            raise DeployError("deploy.venv", f"{venv} is a real directory, not a symlink")
    project = tools.project_dir(profile)
    if project_sha:
        check_sha("--project-sha", project_sha)
    land = _land_table(profile)
    remote, branch = str(land.get("remote", "origin")), str(land.get("branch", "main"))
    revision, text = requirements_of(project, remote, branch, project_sha)
    packages = project_requirements(text, f"{project} requirements.txt at {revision[:12]}")
    requirements, moves = plan(decomp, wanted, fetch)
    findings = [
        {
            "key": "deploy.pin_must_move",
            "blocking": True,
            "message": f"{m['file']}: {m['pin']} is {m['now']} and must be {m['must']} ({m['why']})",
            **m,
        }
        for m in moves
    ]
    result: dict[str, Any] = {
        "decomp": decomp,
        "requirements": [f"{r['name']} @ git+{r['url']}@{r['commit']}" for r in requirements],
        "project_revision": revision,
        "packages": packages,
        "pins_to_move": moves,
        "venvs": [],
    }
    if moves:
        return result, 1, findings
    checkouts, refused = checkout_steps(profile, requirements)
    if refused:
        return result, 1, refused
    result["checkouts"], findings = align_checkouts(checkouts, run)
    if findings:
        return result, 1, findings
    for live in venvs:
        venv = versioned_path(live, decomp)
        steps = install(venv, live, requirements, run, packages)
        row: dict[str, Any] = {"venv": str(live), "versioned": str(venv), "steps": steps}
        result["venvs"].append(row)
        if all(step["ok"] for step in steps):
            row["smoke"] = smoke(venv, project, tools.host_file(profile), run, packages)
            bad = [check for check in row["smoke"] if not check["ok"]]
            findings += [
                {"key": "deploy.smoke", "blocking": True, "message": f"{venv}: {c['check']} failed: {c['tail']}"}
                for c in bad
            ]
        else:
            failed = next(step for step in steps if not step["ok"])
            bad = [failed]
            findings.append(
                {
                    "key": "deploy.install",
                    "blocking": True,
                    "message": f"{venv}: {failed['step']} failed: {failed['tail']}",
                }
            )
        if bad:
            shutil.rmtree(venv, ignore_errors=True)  # our own half-built venv, never linked, so nothing uses it
            continue
        try:
            row["swap"] = point(live, venv)
        except DeployError as error:
            findings.append({"key": error.key, "blocking": True, "message": str(error)})
            shutil.rmtree(venv, ignore_errors=True)  # built but never linked, so nothing uses it
            continue
        row["swap"]["readback_smoke"] = smoke(live, project, tools.host_file(profile), run, packages)
        if not row["swap"]["readback"] or not all(c["ok"] for c in row["swap"]["readback_smoke"]):
            findings.append(
                {"key": "deploy.readback", "blocking": True, "message": f"{live} does not read back to {venv}"}
            )
            continue
        row["resync"] = resync(live, tools.host_file(profile), project, run)
    return result, int(bool(findings)), findings
