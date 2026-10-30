#!/usr/bin/env python3
"""Deploys the landed AbuDecomp64 commit to the rig: `.deploy` runs this after Land, from a clean checkout, with the
landed SHA in LANDED_Projects_AbuN64_AbuDecomp64. Any refusal or failed step prints one line on stderr and exits 1.

  1. the SHA must be on gitea main and on github main (else nothing touches the rig);
  2. `decomp tools deploy --sha SHA --venv ~/venv-decomp-stage` through the rig's build slot installs the pins of
     AbuDecomp64's pyproject at SHA (no --unbake, --recipe or --taste) into a fresh ~/venv-decomp-stage.<sha12> and
     swaps the stage link; ~/venv-decomp is then repointed to the same directory by rename. The engine builds
     `<live>.<sha12>` itself, so one deploy gives one directory for both links. Previous venv directories stay;
  3. when both links already read back to that directory the deploy is skipped;
  4. readback, always: both links resolve to the directory, and `make check` on a fresh RageWars worktree at gitea main
     exits 0 (the 5 ROMs are built and their SHA-1 asserted), set up as ~/finish/maincheck.sh does."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

ENV = "LANDED_Projects_AbuN64_AbuDecomp64"
RIG = "abu@192.168.0.101"
RIG_HOME = "/home/abu"
REMOTES = {
    "gitea": "http://10.10.10.130:18556/Projects/AbuDecomp64.git",
    "github": "https://github.com/AbuN64/abu-decomp-64.git",
}
LINKS = ("venv-decomp", "venv-decomp-stage")
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


class Refused(Exception):
    """One line cause of a refusal or failed step."""


def run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def ssh(command: str) -> tuple[int, str]:
    return run(["ssh", "-o", "BatchMode=yes", RIG, command])


def git(*args: str) -> tuple[int, str]:
    return run(["git", *args])


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


def deploy(sha: str, ssh: Seam, git: Seam) -> str:
    """The whole mechanism; returns the log line to print. Raises Refused on the first failed step."""
    target = f"{RIG_HOME}/venv-decomp-stage.{sha[:12]}"
    check_on_main(sha, git)
    links = read_links(ssh)
    if all(path == target for path in links.values()):
        print(f"{sha[:12]}: both links already read {target}, deploy skipped")
    else:
        if links["venv-decomp-stage"] != target:
            code, out = ssh(deploy_command(sha))
            if code:
                raise Refused(f"decomp tools deploy exit {code}: {last_line(out, 'no output')}")
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
    return f"{sha[:12]}: both links read {target}; {last_line(out, '')}"


def main(environ: dict[str, str] | None = None) -> int:
    try:
        print(deploy(landed_sha(dict(os.environ) if environ is None else environ), ssh, git))
    except Refused as refusal:
        print(f"deploy refused: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
