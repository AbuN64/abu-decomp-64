#!/usr/bin/env python3
"""Deploys the landed AbuDecomp64 commit to the rig: `.deploy` runs this after Land, from a clean checkout, with the
landed SHA in LANDED_Projects_AbuN64_AbuDecomp64. Any refusal or failed step prints one line on stderr and exits 1.

  1. the SHA must be on gitea main and on github main (else nothing touches the rig);
  2. `decomp tools deploy --sha SHA --venv ~/venv-decomp-stage` through the rig's build slot installs the pins of
     AbuDecomp64's pyproject at SHA (no --unbake, --recipe or --taste) into a fresh ~/venv-decomp-stage.<sha12> and
     swaps the stage link. The engine builds `<live>.<sha12>` itself, so one deploy gives one directory for both links;
  3. the suite gate, always, between the build and the swap: the full test suite (pytest over tests/, unittest and
     pytest modules) runs through ~/finish/slot on a clean checkout of exactly SHA, with the freshly built venv's
     python and the checkout's src first on the import path. The imported abu_decomp path is printed and must lie
     inside the checkout. Any failure refuses (one line naming the first failing test) before ~/venv-decomp moves;
     the summary line, the SHA and the imported path go into the deploy log and into <venv>/suite.txt;
  4. ~/venv-decomp is repointed to the same directory by rename. Previous venv directories stay; when both links
     already read that directory, the build and the repoint are skipped (the suite gate still runs);
  5. readback, always: both links resolve to the directory, and `make check` on a fresh RageWars worktree at gitea main
     exits 0 (the 5 ROMs are built and their SHA-1 asserted), set up as ~/finish/maincheck.sh does."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import PurePosixPath

ENV = "LANDED_Projects_AbuN64_AbuDecomp64"
RIG = "abu@192.168.0.101"
RIG_HOME = "/home/abu"
REMOTES = {
    "gitea": "http://10.10.10.130:18556/Projects/AbuDecomp64.git",
    "github": "https://github.com/AbuN64/abu-decomp-64.git",
}
LINKS = ("venv-decomp", "venv-decomp-stage")
PACKAGE = "abu_decomp"
SHA = re.compile(r"[0-9a-f]{40}")

# ssh(command) runs one shell line on the rig and git(args) runs git here; both return (exit code, output).
Seam = Callable[..., "tuple[int, str]"]

READBACK = r"""set -u
repo=$HOME/code/ragewars-decomp
wt=$HOME/code/wt/DEPLOY-READBACK
git -C "$repo" fetch -q gitea main || exit 9
sha=$(git -C "$repo" rev-parse FETCH_HEAD) || exit 9
git -C "$repo" worktree remove --force "$wt" 2>/dev/null
git -C "$repo" worktree add -q --detach "$wt" "$sha" || exit 9
cd "$wt" || exit 9
ln -s "$repo/roms" roms
export PATH=$HOME/venv-decomp/bin:$PATH
step() {
  name=$1; shift
  "$@" > "$name.log" 2>&1 || { echo "readback: $* failed at RageWars $sha:"; tail -n 5 "$name.log"; exit 1; }
}
step setup unbake setup --no-make
step extract make extract
step check make -k -j8 check
git -C "$repo" worktree remove --force "$wt"
echo "readback: make check exit 0 at RageWars gitea main $sha"
"""

# suite SHA URL PYTHON DEST: a clean checkout of exactly SHA, the suite run in it, one `suite-KEY: value` line per fact.
SUITE = r"""set -u
sha=$1 url=$2 py=$3 dest=$4
work=$(mktemp -d "$HOME/finish/DEPLOY-SUITE.XXXXXX") || exit 9
trap 'rm -rf "$work"' EXIT
log=$HOME/finish/deploy-suite-${sha:0:12}.log
git -c init.templateDir= init -q "$work/checkout" || exit 9
cd "$work/checkout" || exit 9
git fetch -q "$url" main || { echo "suite-error: cannot fetch main from $url" >&2; exit 9; }
git checkout -q --detach "$sha" || { echo "suite-error: cannot check out $sha" >&2; exit 9; }
echo "suite-checkout: $(pwd -P)"
echo "suite-sha: $(git rev-parse HEAD)"
cat > "$work/runner.py" <<'PY'
__RUNNER__
PY
mkdir .tmp
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=init.templateDir GIT_CONFIG_VALUE_0=
export TMPDIR=$PWD/.tmp ABU_SLOTS_DIR=$(mktemp -d "$work/slots.XXXXXX")
export PYTHONPATH=$PWD/src PYTHONDONTWRITEBYTECODE=1
rc=0
"$py" "$work/runner.py" > "$log" 2>&1 || rc=$?
grep '^suite-' "$log"
echo "suite-log: $log"
if [ "$rc" = 0 ]; then
  { echo "suite-sha: $(git rev-parse HEAD)"; grep '^suite-\(summary\|import\)' "$log"; } > "$dest/suite.txt" || exit 9
fi
exit "$rc"
"""

# The runner: puts the checkout's src first, proves the package comes from there, runs pytest over tests/ and reports.
RUNNER = r"""import os
import sys
import time

root = os.path.realpath(os.getcwd())
sys.path.insert(0, os.path.join(root, "src"))
PACKAGE = "abu_decomp"


def imported():
    return sorted({os.path.realpath(m.__file__) for n, m in sys.modules.items()
                   if (n == PACKAGE or n.startswith(PACKAGE + ".")) and getattr(m, "__file__", None)})


def inside(path):
    return path.startswith(root + os.sep)


__import__(PACKAGE)
print("suite-import:", os.path.realpath(sys.modules[PACKAGE].__file__), flush=True)
import pytest  # noqa: E402


class Tally:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0
        self.first = None

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed += 1
            self.first = self.first or report.nodeid
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
code = int(pytest.main(["-q", "-p", "no:cacheprovider", "-rfE", "--tb=short", "tests"], plugins=[tally]))
seconds = time.monotonic() - start
outside = [path for path in imported() if not inside(path)]
if outside:
    print("suite-import:", outside[0], flush=True)
    sys.exit(3)
print(f"suite-summary: {tally.passed} passed, {tally.failed} failed, {tally.skipped} skipped in {seconds:.0f} s")
if code or tally.failed or not tally.passed:
    print("suite-first-failure:", tally.first or f"pytest exit {code}, {tally.passed} passed")
    sys.exit(1)
"""


class Refused(Exception):
    """One line cause of a refusal or failed step."""


def run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def ssh(command: str) -> tuple[int, str]:
    return run(["ssh", "-o", "BatchMode=yes", RIG, command])


def git(*args: str) -> tuple[int, str]:
    return run(["git", *args])


def suite_command(sha: str, python: str, dest: str, url: str = REMOTES["gitea"]) -> str:
    script = shlex.quote(SUITE.replace("__RUNNER__", RUNNER))
    return f"~/finish/slot bash -c {script} suite {sha} {url} {python} {dest}"


def landed_sha(environ: dict[str, str]) -> str:
    sha = environ.get(ENV, "")
    if not SHA.fullmatch(sha):
        raise Refused(f"{ENV} is not a 40-hex SHA: {sha!r}")
    return sha


def last_line(text: str, default: object) -> str:
    return text.splitlines()[-1] if text else str(default)


def check_on_main(sha: str, git: Seam) -> None:
    """Refuse unless sha is an ancestor of main on gitea and on github (fetched into a scratch bare repo)."""
    work = tempfile.mkdtemp(prefix="deploy-rig-")
    try:
        git("-C", work, "init", "-q", "--bare")
        for name, url in REMOTES.items():
            code, out = git("-C", work, "fetch", "-q", "--no-tags", "--filter=blob:none", url, "main")
            if code:
                raise Refused(f"cannot fetch {name} main ({url}): {last_line(out, code)}")
            if git("-C", work, "merge-base", "--is-ancestor", sha, "FETCH_HEAD")[0]:
                raise Refused(f"{sha[:12]} is not on {name} main")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def read_links(ssh: Seam) -> dict[str, str]:
    code, out = ssh("readlink -f " + " ".join(f"~/{link}" for link in LINKS))
    paths = out.splitlines()
    if code or len(paths) != len(LINKS):
        raise Refused(f"cannot read the rig venv links: {last_line(out, code)}")
    return dict(zip(LINKS, paths, strict=True))


def deploy_command(sha: str) -> str:
    return f"~/finish/slot decomp tools deploy --sha {sha} --venv ~/venv-decomp-stage"


def suite_facts(out: str) -> dict[str, str]:
    facts = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith("suite-"):
            facts[key] = value
    return facts


def suite(sha: str, target: str, ssh: Seam) -> list[str]:
    """The full suite at exactly sha on a clean rig checkout; returns the log lines, raises Refused on any failure."""
    code, out = ssh(suite_command(sha, f"{target}/bin/python", target))
    facts = suite_facts(out)
    for key in ("suite-checkout", "suite-sha", "suite-import"):
        if key not in facts:
            raise Refused(f"suite gate did not start at {sha[:12]} (exit {code}): {last_line(out, 'no output')}")
    if facts["suite-sha"] != sha:
        raise Refused(f"suite ran at {facts['suite-sha']}, not the landed {sha}")
    if not PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]):
        raise Refused(
            f"suite imported {PACKAGE} from {facts['suite-import']}, outside its checkout {facts['suite-checkout']}"
        )
    if code or "suite-first-failure" in facts or "suite-summary" not in facts:
        first = facts.get("suite-first-failure") or f"no summary, exit {code}: {last_line(out, 'no output')}"
        summary, log = facts.get("suite-summary", "no summary"), facts.get("suite-log", "no log")
        raise Refused(f"suite failed at {first}: {summary} ({log})")
    return [
        f"{sha[:12]}: suite {facts['suite-summary']}",
        f"{sha[:12]}: suite sha {facts['suite-sha']}",
        f"{sha[:12]}: suite imported {facts['suite-import']}",
    ]


def deploy(sha: str, ssh: Seam, git: Seam) -> list[str]:
    """The whole mechanism; returns the log lines to print. Raises Refused on the first failed step."""
    target = f"{RIG_HOME}/venv-decomp-stage.{sha[:12]}"
    check_on_main(sha, git)
    links = read_links(ssh)
    lines = []
    skipped = all(path == target for path in links.values())
    if skipped:
        lines.append(f"{sha[:12]}: both links already read {target}, build and swap skipped")
    elif links["venv-decomp-stage"] != target:
        code, out = ssh(deploy_command(sha))
        if code:
            raise Refused(f"decomp tools deploy exit {code}: {last_line(out, 'no output')}")
    lines += suite(sha, target, ssh)
    if not skipped:
        code, out = ssh(
            f"test -x {target}/bin/python && ln -sfn {target} ~/.venv-decomp.new"
            " && mv -T ~/.venv-decomp.new ~/venv-decomp"
        )
        if code:
            raise Refused(f"cannot repoint ~/venv-decomp to {target}: {last_line(out, code)}")
    wrong = {link: path for link, path in read_links(ssh).items() if path != target}
    if wrong:
        raise Refused(f"readback: {wrong} must read {target}")
    code, out = ssh(f"~/finish/slot bash -c {shlex.quote(READBACK)}")
    if code:
        raise Refused(out.splitlines()[0] if out else f"readback: make check exit {code}")
    return [*lines, f"{sha[:12]}: both links read {target}; {last_line(out, '')}"]


def main(environ: dict[str, str] | None = None) -> int:
    try:
        for line in deploy(landed_sha(dict(os.environ) if environ is None else environ), ssh, git):
            print(line, flush=True)
    except Refused as refusal:
        print(f"deploy refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
