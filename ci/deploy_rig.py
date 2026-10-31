#!/usr/bin/env python3
"""Deploys the landed AbuDecomp64 commit to the rig through Job Invoke. Run from a run's AbuDecomp64 worktree, whose
card has a live run (Job Invoke pushes the commit from there):

  LANDED_Projects_AbuN64_AbuDecomp64=SHA ci/deploy_rig.py --board BOARD --card CARD --toolkit /abs/live/venv

Any refusal or failed step prints one line on stderr and exits 1. Every rig step is one `mo-butlermaster Job Invoke`
at the exact SHA in a Worktree (the job runs in <Root>/<board>.<card>/<repo> with JOBS set to Dos's width):

  1. the SHA to deploy must be on gitea main and on github main (else nothing touches the rig); when gitea main has
     moved past it to a descendant, main's head is deployed instead and the log says the older deploy deferred;
  2. the activation job (Toolkit = the live link, Revision = the SHA's first 12 digits, so BUILD is <toolkit>.<sha12>):
     `decomp tools deploy --sha SHA` installs the pins of AbuDecomp64's pyproject at SHA into a fresh venv (through a
     private `<toolkit>-stage` link the engine swaps; the activation's single per-host permit keeps it race free),
     BUILD is linked to that venv, and the full test suite (pytest over tests/, unittest and pytest modules) runs
     in the Worktree with BUILD's python and the Worktree's src first on the import path. The imported abu_decomp
     path must lie inside the Worktree. A failing suite exits non-zero: Job Invoke leaves the live and previous links
     alone and the deploy refuses with one line naming the first failing test. On exit 0 Job Invoke switches the
     live link to BUILD and keeps the replaced venv as <toolkit>.previous;
  3. the readback job: the live link resolves to BUILD, and the installed commit of AbuDecomp64 and of every direct
     git pin of its pyproject at SHA equals the pinned one. Any mismatch refuses (the live link stays as switched:
     fix forward, no rollback).
The deploy's answer is the deployed SHA with each job's Exit and Log."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

ENV = "LANDED_Projects_AbuN64_AbuDecomp64"
REPOSITORY = "Projects/AbuN64/AbuDecomp64"
HOST = "rig"
REMOTES = {
    "gitea": "http://10.10.10.130:18556/Projects/AbuDecomp64.git",
    "github": "https://github.com/AbuN64/abu-decomp-64.git",
}
PACKAGE = "abu_decomp"
SHA = re.compile(r"[0-9a-f]{40}")

# git(args) runs git here and returns (exit code, output); job(request) is `mo-butlermaster Job Invoke` and returns its
# answer; read(path) reads a job's Log, which lives on this machine.
Seam = Callable[..., "tuple[int, str]"]
Job = Callable[[dict], "Answer"]
Read = Callable[[str], str]

# BUILD runs inside the activation job with BUILD (<toolkit>.<sha12>) set by Job Invoke: `decomp tools deploy` builds
# `<toolkit>-stage.<sha12>` (or -N when that name is taken) and swaps only the private stage link; BUILD becomes a
# link to that venv, so a failed gate leaves the live and previous links unchanged and Job Invoke's switch is the only
# way a venv goes live. Arguments: SHA TOOLKIT.
BUILD = r"""set -u
sha=$1 toolkit=$2
: "${BUILD:?Job Invoke sets BUILD for an activation}"
stage=$toolkit-stage
(cd "$HOME" && "$toolkit/bin/decomp" tools deploy --sha "$sha" --venv "$stage") \
  || { echo "deploy-error: decomp tools deploy exit $?" >&2; exit 1; }
built=$(readlink -f "$stage")
case $built in
  "$stage.${sha:0:12}" | "$stage.${sha:0:12}"-[0-9]*) ;;
  *) echo "deploy-error: $stage reads $built, not a build of $sha" >&2; exit 1 ;;
esac
ln -sfn "$built" "$BUILD" || exit 1
echo "deploy-build: $built"
"""

# SUITE runs in the Worktree after BUILD, with built and BUILD set: one `suite-KEY: value` line per fact, then the
# suite's own output.
SUITE = r"""set -u
work=$(mktemp -d) || exit 9
trap 'rm -rf "$work"' EXIT
echo "suite-checkout: $(pwd -P)"
echo "suite-sha: $(git rev-parse HEAD)"
cat > "$work/runner.py" <<'PY'
__RUNNER__
PY
rm -rf .tmp && mkdir .tmp
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=init.templateDir GIT_CONFIG_VALUE_0=
export TMPDIR=$PWD/.tmp ABU_SLOTS_DIR=$(mktemp -d "$work/slots.XXXXXX")
export PYTHONPATH=$PWD/src PYTHONDONTWRITEBYTECODE=1
rc=0
"$BUILD/bin/python" "$work/runner.py" > "$work/suite.log" 2>&1 || rc=$?
cat "$work/suite.log"
if [ "$rc" = 0 ]; then
  { echo "suite-sha: $(git rev-parse HEAD)"; grep '^suite-\(summary\|import\)' "$work/suite.log"; } \
    > "$built/suite.txt" || exit 9
fi
exit "$rc"
"""

# READBACK runs in the Worktree after the switch. Arguments: SHA TOOLKIT BUILD.
READBACK = r"""set -u
sha=$1 toolkit=$2 build=$3
live=$(readlink -f "$toolkit") || exit 9
want=$(readlink -f "$build") || exit 9
[ "$live" = "$want" ] || { echo "readback: $toolkit reads $live, not $build ($want)" >&2; exit 1; }
"$toolkit/bin/python" - "$sha" <<'PY'
import importlib.metadata
import json
import re
import sys
import tomllib

sha = sys.argv[1]
PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*@\s*git\+(?P<url>https://[^@\s]+)@(?P<commit>[0-9a-f]{40})$")
with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]
pins = {project["name"]: sha}
for row in project.get("dependencies", []):
    match = PIN.match(row.strip())
    if match:
        pins[match["name"]] = match["commit"]
bad = []
for name, commit in sorted(pins.items()):
    try:
        direct = json.loads(importlib.metadata.distribution(name).read_text("direct_url.json") or "{}")
        installed = direct.get("vcs_info", {}).get("commit_id", "none")
    except importlib.metadata.PackageNotFoundError:
        installed = "not installed"
    print(f"readback-pin: {name} {installed}", flush=True)
    if installed != commit:
        bad.append(f"{name} is {installed}, the pin is {commit}")
if bad:
    sys.exit("readback: " + "; ".join(bad))
PY
"""

# The runner: puts the checkout's src first, proves the package comes from there, runs pytest over tests/ and reports.
RUNNER = r"""import os
import sys
import time

root = os.path.realpath(os.getcwd())
sys.path.insert(0, os.path.join(root, "src"))
PACKAGE = "abu_decomp"


def emit(line):
    # a test may leave its own output without a newline: every fact starts on a fresh line
    sys.stdout.write("\n" + line + "\n")
    sys.stdout.flush()


def imported():
    return sorted({os.path.realpath(m.__file__) for n, m in sys.modules.items()
                   if (n == PACKAGE or n.startswith(PACKAGE + ".")) and getattr(m, "__file__", None)})


def inside(path):
    return path.startswith(root + os.sep)


__import__(PACKAGE)
emit("suite-import: " + os.path.realpath(sys.modules[PACKAGE].__file__))
import pytest  # noqa: E402


class Tally:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0
        self.first = None

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.first = self.first or report.nodeid
        if hasattr(report, "context"):  # a subtest report: its test has its own report, pytest counts them apart
            return
        if report.failed:
            self.failed += 1
        elif report.skipped and report.when != "teardown":
            self.skipped += 1
        elif report.passed and report.when == "call":
            self.passed += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed += 1
            self.first = self.first or report.nodeid


tally = Tally()
start = time.monotonic()
# rootdir and config pinned to the checkout: node ids stay checkout-relative wherever the checkout sits
config = os.path.join(root, "pyproject.toml")
args = ["-q", "-p", "no:cacheprovider", "-rfE", "--tb=short", "--rootdir", root, "-c", config]
code = int(pytest.main(args + ["tests"], plugins=[tally]))
seconds = time.monotonic() - start
outside = [path for path in imported() if not inside(path)]
if outside:
    emit("suite-import: " + outside[0])
    sys.exit(3)
emit(f"suite-summary: {tally.passed} passed, {tally.failed} failed, {tally.skipped} skipped in {seconds:.0f} s")
if code or tally.first or not tally.passed:
    emit("suite-first-failure: " + (tally.first or f"pytest exit {code}, {tally.passed} passed"))
    sys.exit(1)
"""


class Refused(Exception):
    """One line cause of a refusal or failed step."""


class Answer(dict):
    """The Response of one Job Invoke: Exit, Log, Width, Host and Subject."""


def run(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def git(*args: str) -> tuple[int, str]:
    return run(["git", *args])


def read_log(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError as error:
        raise Refused(f"cannot read the job log {path}: {error}") from error


def last_line(text: str, default: object) -> str:
    return text.strip().splitlines()[-1] if text.strip() else str(default)


def answer_of(code: int, out: str) -> Answer:
    """Parses `Job Invoke --output=json`: the Response, or Refused with the refusal's Code, Message and NextStep."""
    try:
        response = json.loads(out)["Response"]
    except (ValueError, KeyError, TypeError):
        raise Refused(f"Job Invoke answered no Response (exit {code}): {last_line(out, 'no output')}") from None
    error = response.get("Error") if isinstance(response, dict) else None
    if error or code:
        error = error or {}
        raise Refused(
            f"Job Invoke refused {error.get('Code', 'Fault')} (exit {code}): {error.get('Message', 'no message')}"
            + (f"; next: {error['NextStep']}" if error.get("NextStep") else "")
        )
    if not isinstance(response.get("Exit"), int) or not isinstance(response.get("Log"), str):
        raise Refused(f"Job Invoke answered no Exit and Log: {json.dumps(response)[:200]}")
    return Answer(response)


def job_invoke(body: dict) -> Answer:
    """The public CLI: `mo-butlermaster Job Invoke --input=json` with the request on stdin."""
    stdin = json.dumps({"Metadata": {}, "Request": body})
    return answer_of(*run(["mo-butlermaster", "Job", "Invoke", "--input=json", "--output=json"], stdin))


def landed_sha(environ: dict[str, str]) -> str:
    sha = environ.get(ENV, "")
    if not SHA.fullmatch(sha):
        raise Refused(f"{ENV} is not a 40-hex SHA: {sha!r}")
    return sha


def newest_main(sha: str, git: Seam) -> str:
    """The SHA to deploy: gitea main's head when it descends from sha (sha itself when it is the head), else Refused.
    The head must also be on github main. Both mains are fetched into a scratch bare repo."""
    work = tempfile.mkdtemp(prefix="deploy-rig-")
    try:
        git("-C", work, "init", "-q", "--bare")

        def fetch(name: str) -> None:
            code, out = git("-C", work, "fetch", "-q", "--no-tags", "--filter=blob:none", REMOTES[name], "main")
            if code:
                raise Refused(f"cannot fetch {name} main ({REMOTES[name]}): {last_line(out, code)}")

        def require_on(name: str, commit: str) -> None:
            if git("-C", work, "merge-base", "--is-ancestor", commit, "FETCH_HEAD")[0]:
                raise Refused(f"{commit[:12]} is not on {name} main")

        fetch("gitea")
        require_on("gitea", sha)
        code, out = git("-C", work, "rev-parse", "FETCH_HEAD")
        head = out if code == 0 and SHA.fullmatch(out) else sha
        fetch("github")
        require_on("github", head)
        return head
    finally:
        shutil.rmtree(work, ignore_errors=True)


def have_commit(sha: str, git: Seam) -> None:
    """Job Invoke pushes the commit from this repository: fetch gitea main when the commit is not here yet."""
    if git("cat-file", "-e", f"{sha}^{{commit}}")[0]:
        git("fetch", "-q", "gitea", "main")
    if git("cat-file", "-e", f"{sha}^{{commit}}")[0]:
        raise Refused(f"{sha[:12]} is not in this repository, so Job Invoke cannot push it to the rig")


def request(board: str, card: str, sha: str, argv: list[str], activation: dict[str, str] | None = None) -> dict:
    body = {"Subject": {"Board": board, "Card": card}, "Host": HOST, "Worktree": f"{REPOSITORY}@{sha}", "Argv": argv}
    return {**body, "Activation": activation} if activation else body


def suite_facts(out: str) -> dict[str, str]:
    facts = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith("suite-"):
            facts[key] = value
    return facts


def suite(sha: str, log: str, text: str) -> list[str]:
    """The suite facts of the activation job's log; returns the log lines, raises Refused when any is wrong."""
    facts = suite_facts(text)
    for key in ("suite-checkout", "suite-sha", "suite-import", "suite-summary"):
        if key not in facts:
            raise Refused(f"suite gate did not finish at {sha[:12]} ({log}): {last_line(text, 'no output')}")
    if facts["suite-sha"] != sha:
        raise Refused(f"suite ran at {facts['suite-sha']}, not the landed {sha}")
    if not PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]):
        raise Refused(
            f"suite imported {PACKAGE} from {facts['suite-import']}, outside its checkout {facts['suite-checkout']}"
        )
    if "suite-first-failure" in facts:
        raise Refused(f"suite failed at {facts['suite-first-failure']}: {facts['suite-summary']} ({log})")
    return [
        f"{sha[:12]}: suite {facts['suite-summary']}",
        f"{sha[:12]}: suite sha {facts['suite-sha']}",
        f"{sha[:12]}: suite imported {facts['suite-import']}",
    ]


def deploy(sha: str, board: str, card: str, toolkit: str, job: Job, git: Seam, read: Read) -> list[str]:
    """Deploys the newest landed main; returns the log lines to print. Raises Refused on the first failed step."""
    lines = []
    head = newest_main(sha, git)
    if head != sha:
        lines.append(f"{sha[:12]}: main has moved to {head[:12]}, deploying main's head; the older deploy deferred")
        sha = head
    have_commit(sha, git)
    revision = sha[:12]
    build = f"{toolkit}.{revision}"
    script = BUILD + SUITE.replace("__RUNNER__", RUNNER)
    argv = ["bash", "-c", script, "deploy", sha, toolkit]
    activation = job(request(board, card, sha, argv, {"Toolkit": toolkit, "Revision": revision}))
    log = activation["Log"]
    lines.append(f"{revision}: activation job Exit {activation['Exit']}, Log {log}")
    text = read(log)
    if activation["Exit"]:
        raise Refused(f"activation exit {activation['Exit']}, links unchanged: {last_line(text, 'no output')} ({log})")
    lines += suite(sha, log, text)
    readback = job(request(board, card, sha, ["bash", "-c", READBACK, "readback", sha, toolkit, build]))
    lines.append(f"{revision}: readback job Exit {readback['Exit']}, Log {readback['Log']}")
    final = read(readback["Log"])
    if readback["Exit"]:
        raise Refused(f"readback exit {readback['Exit']}: {last_line(final, 'no output')} ({readback['Log']})")
    return [*lines, f"{revision}: {toolkit} reads {build}; {last_line(final, '')}", f"deployed {sha}"]


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the landed AbuDecomp64 commit to the rig through Job Invoke.")
    for name in ("board", "card", "toolkit"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        sha = landed_sha(dict(os.environ) if environ is None else environ)
        if not os.path.isabs(args.toolkit) or os.path.normpath(args.toolkit) != args.toolkit:
            raise Refused(f"--toolkit must be a clean absolute path: {args.toolkit!r}")
        for line in deploy(sha, args.board, args.card, args.toolkit, job_invoke, git, read_log):
            print(line, flush=True)
    except Refused as refusal:
        print(f"deploy refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
