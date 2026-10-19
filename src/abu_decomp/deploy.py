"""`decomp tools deploy`: install one decomp commit and its tools into venvs, after the pins agree.

The tools are installed from git commits only (`name @ git+URL@SHA`, a full 40-hex SHA; a wheel or a branch is
refused), so a venv's provenance is always a commit that `decomp dependencies` can check. Before anything is
installed the pins that must agree are compared and the ones that would have to move are printed:

  decomp's pyproject pins of unbake, Recipe Maker and Cake Taste (at DECOMP_SHA) against the commits asked for;
  Recipe Maker's own pin of unbake (at its commit) against the unbake commit;
  the project's pin file (`tools.pin_file`, default ci/run) against the version each commit declares.

Any mismatch refuses the deploy and names the file, the pin, what it says now and what it must say. When they agree,
each checkout the seat names in `tools.checkouts` (a tool's code run from a git checkout) is brought to the pinned
commit (fetch, detached checkout) and refused, untouched, when it has local changes. Each venv then gets the four
requirements (the old ones uninstalled first, so pip cannot keep a stale commit), then a smoke test:
`decomp dependencies` (provenance) and `unbake symbols --version V` on the project.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tools
from abu_decomp.config import Profile

DECOMP_URL = "https://github.com/AbuN64/abu-decomp-64"
SHA = re.compile(r"[0-9a-f]{40}")
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*@\s*git\+(?P<url>https://[^@\s]+)@(?P<commit>[0-9a-f]{40})$")

Fetch = Callable[[str, str, str], str]
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


def version_of(pyproject: str, url: str, sha: str, fetch: Fetch) -> str:
    """The version a pyproject declares: its `version`, or the `__version__` of the module a setuptools dynamic
    version names (`attr = "pkg.__version__"`, read from the package's __init__.py at the same commit)."""
    document = tomllib.loads(pyproject)
    if version := document.get("project", {}).get("version"):
        return str(version)
    attr = document.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {}).get("attr", "")
    module = attr.rpartition(".")[0].replace(".", "/")
    if not module:
        return ""
    for base in ("src/", ""):
        try:
            text = fetch(url, sha, f"{base}{module}/__init__.py")
        except DeployError:
            continue
        if found := re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.M):
            return found[1]
    return ""


def check_sha(label: str, value: str) -> str:
    if not SHA.fullmatch(value):
        raise DeployError(
            "deploy.sha", f"{label} must be a full 40-hex commit SHA (git+URL@SHA installs only), got {value!r}"
        )
    return value


def plan(
    decomp: str, wanted: dict[str, str], fetch: Fetch, pin_file: Path | None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(requirements to install, pins that must move). `wanted` maps tool command to the commit asked for."""
    own = pins_of(fetch(DECOMP_URL, decomp, "pyproject.toml"))
    moves: list[dict[str, str]] = []
    requirements = [{"name": tools.DISTRIBUTION, "url": DECOMP_URL, "commit": decomp}]
    versions: dict[str, str] = {}
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
        versions[command] = version_of(tool_pyprojects[command], pin["url"], commit, fetch)
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
    if pin_file is not None:
        for command, version in tools.project_pins(pin_file).items():
            if versions.get(command) != version:
                moves.append(
                    {
                        "file": str(pin_file),
                        "pin": command,
                        "now": version,
                        "must": versions.get(command, "?"),
                        "why": f"the {command} commit being deployed declares version {versions.get(command)}",
                    }
                )
    return requirements, moves


def install(venv: Path, requirements: Sequence[dict[str, str]], run: Run) -> list[dict[str, Any]]:
    python = venv / "bin" / "python"
    names = [r["name"] for r in requirements]
    steps = [
        ("uninstall", [str(python), "-m", "pip", "uninstall", "-y", *names]),
        (
            "install",
            [
                str(python),
                "-m",
                "pip",
                "install",
                *(f"{r['name']} @ git+{r['url']}@{r['commit']}" for r in requirements),
            ],
        ),
    ]
    done: list[dict[str, Any]] = []
    for name, argv in steps:
        code, output = run(argv, None)
        done.append({"step": name, "ok": code == 0, "tail": output[-400:] if code else ""})
        if code and name == "install":
            break
    return done


def smoke(venv: Path, project: Path | None, host: Path, run: Run) -> list[dict[str, Any]]:
    """`decomp dependencies` (every installed tool at its pinned commit) and `unbake symbols --version V` on the
    project, V being the first version directory of the project."""
    results = []
    code, output = run([str(venv / "bin" / "decomp"), "dependencies"], None)
    results.append({"check": "decomp dependencies", "ok": code == 0, "tail": output[-300:] if code else ""})
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


def deploy(
    profile: Profile,
    decomp: str,
    venvs: Sequence[Path],
    commits: dict[str, str | None],
    *,
    fetch: Fetch = fetch_file,
    run: Run = run_command,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    check_sha("--sha", decomp)
    wanted = {command: check_sha(f"--{command}", sha) for command, sha in commits.items() if sha}
    if not venvs:
        raise DeployError("deploy.venv", "pass at least one --venv")
    for venv in venvs:
        if not (venv / "bin" / "python").is_file():
            raise DeployError("deploy.venv", f"{venv} is not a venv (no bin/python)")
    project = tools.project_dir(profile)
    requirements, moves = plan(decomp, wanted, fetch, tools.pin_file_of(profile, project))
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
        "pins_to_move": moves,
        "venvs": [],
    }
    if moves:
        return result, 1, findings
    wanted, refused = checkout_steps(profile, requirements)
    if refused:
        return result, 1, refused
    result["checkouts"], findings = align_checkouts(wanted, run)
    if findings:
        return result, 1, findings
    for venv in venvs:
        steps = install(venv, requirements, run)
        row: dict[str, Any] = {"venv": str(venv), "steps": steps}
        if all(step["ok"] for step in steps):
            row["smoke"] = smoke(venv, project, tools.host_file(profile), run)
            for check in row["smoke"]:
                if not check["ok"]:
                    findings.append(
                        {
                            "key": "deploy.smoke",
                            "blocking": True,
                            "message": f"{venv}: {check['check']} failed: {check['tail']}",
                        }
                    )
        else:
            failed = next(step for step in steps if not step["ok"])
            findings.append(
                {
                    "key": "deploy.install",
                    "blocking": True,
                    "message": f"{venv}: pip {failed['step']} failed: {failed['tail']}",
                }
            )
        result["venvs"].append(row)
    return result, int(bool(findings)), findings
