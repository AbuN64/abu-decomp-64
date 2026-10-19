"""A closing lane's own rig jobs, handed to its successor.

A link ends at its wall or its context cap, but the rig job it started (a score, a permuter run, a build) goes on. The
runner calls `record` as the link closes. It finds the jobs two ways: the ssh processes the link still owns locally
(a call blocked on a rig command), and, on every rig host the link talked to, the process groups that run in or name
a directory the lane owns (the detached jobs it left behind). Each is written, with its pid, process group, age,
command and output files, to JOBS.json and to a section of HANDOFF.md. `decomp lane continue` calls `adopt_text` and
the continuation brief opens with the jobs that still run as "adopt, don't restart", so the successor waits on them
instead of rediscovering or repeating them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

JOBS_FILE = "JOBS.json"
HEADING = "## Running rig jobs (adopt, do not restart)"
# ssh options that take a separate argument; everything else starting with `-` is a flag.
_SSH_ARGUMENT = frozenset("bcDEeFIiJLlmOopQRSWw")
_REDIRECT = re.compile(r"(?<!\d)&?>>?\s*(?!&)([^\s;|&'\"<>()]+)|\btee\s+(?:-\w+\s+)*([^\s;|&'\"<>()-][^\s;|&'\"<>()]*)")
_HOST = re.compile(r"\bssh\b[^\n]{0,160}?\b([A-Za-z_][\w-]*@[\w.-]+)")
Run = Callable[[Sequence[str], float, str], "tuple[int, str]"]

# Runs on the rig (python3 -I, script on stdin, {owned} replaced by a JSON list): the process groups that have a
# directory of `owned` as cwd or in an argument, except this scan and its own ancestors.
REMOTE_SCAN = r"""
import json, os, sys
owned = json.loads(%r)
mine, up = set(), os.getpid()
while up > 1:
    mine.add(up)
    try:
        up = int(open("/proc/%%d/stat" %% up).read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        break
groups = {}
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) in mine:
        continue
    pid = int(name)
    try:
        fields = open("/proc/%%d/stat" %% pid).read().rsplit(")", 1)[1].split()
        argv = open("/proc/%%d/cmdline" %% pid, "rb").read().decode(errors="replace").split("\0")[:-1]
        cwd = os.readlink("/proc/%%d/cwd" %% pid)
    except (OSError, IndexError):
        continue
    if fields[0] == "Z" or not argv or os.path.basename(argv[0]) in ("sshd", "ssh", "sshd-session"):
        continue
    if not any(cwd == o or cwd.startswith(o.rstrip("/") + "/") or any(o in a for a in argv) for o in owned):
        continue
    outs = []
    for fd in (1, 2):
        try:
            target = os.readlink("/proc/%%d/fd/%%d" %% (pid, fd))
        except OSError:
            continue
        gone = target.endswith(" (deleted)")  # an unlinked file (a temp stream) can no longer be read by path
        if target.startswith("/") and not gone and not target.startswith(("/dev/", "/proc/")) and target not in outs:
            outs.append(target)
    row = groups.setdefault(int(fields[2]), {"pgid": int(fields[2]), "pids": [], "command": " ".join(argv)[:400],
                                              "cwd": cwd, "age": 0, "outputs": []})
    row["pids"].append(pid)
    row["outputs"] += [o for o in outs if o not in row["outputs"]]
    if pid == int(fields[2]):
        row["command"], row["cwd"] = " ".join(argv)[:400], cwd
print(json.dumps(sorted(groups.values(), key=lambda r: r["pgid"])))
"""


def run_ssh(argv: Sequence[str], timeout: float, stdin: str = "") -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv), input=stdin or None, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 255, ""
    return done.returncode, done.stdout


def ssh(host: str, *command: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, *command]


def split_ssh(argv: Sequence[str]) -> tuple[str, str]:
    """(host, remote command) of an ssh command line; ("", "") for anything else."""
    if not argv or os.path.basename(argv[0]) != "ssh":
        return "", ""
    index = 1
    while index < len(argv) and argv[index].startswith("-") and len(argv[index]) > 1:
        flag = argv[index]
        index += 2 if len(flag) == 2 and flag[1] in _SSH_ARGUMENT else 1
    if index >= len(argv):
        return "", ""
    return argv[index], " ".join(argv[index + 1 :])


def redirect_targets(command: str) -> list[str]:
    """Files a shell command writes with `>`, `>>`, `&>` or `tee` (`2>&1` is not one)."""
    found = [a or b for a, b in _REDIRECT.findall(command)]
    return list(dict.fromkeys(path for path in found if path and not path.startswith("/dev/")))


def hosts_in(transcript: Path) -> list[str]:
    """The `user@host` targets of the ssh commands the lane ran, from its transcript."""
    try:
        text = transcript.read_text(errors="replace")
    except OSError:
        return []
    return list(dict.fromkeys(_HOST.findall(text)))


def _stat_fields(pid: int, proc: str) -> list[str]:
    return Path(proc, str(pid), "stat").read_text().rsplit(")", 1)[1].split()


def census(pids: Sequence[int], proc: str = "/proc") -> list[dict[str, Any]]:
    """The ssh processes among `pids`: pid, process group, start tick, host, remote command and output paths."""
    jobs: list[dict[str, Any]] = []
    for pid in pids:
        try:
            argv = Path(proc, str(pid), "cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
            fields = _stat_fields(pid, proc)
        except (OSError, IndexError):
            continue
        host, remote = split_ssh(argv)
        if not host:
            continue
        outputs: list[str] = []
        for descriptor in (1, 2):
            try:
                target = os.readlink(f"{proc}/{pid}/fd/{descriptor}")
            except OSError:
                continue
            if target.startswith("/") and not target.startswith(("/dev/", "/proc/")):
                outputs.append(target)
        outputs += redirect_targets(remote)
        jobs.append(
            {
                "kind": "ssh",
                "pid": pid,
                "pgid": int(fields[2]),
                "start": fields[19],
                "host": host,
                "command": remote,
                "outputs": list(dict.fromkeys(outputs)),
            }
        )
    return jobs


def scan_host(host: str, owned: Sequence[str], run: Run = run_ssh) -> list[dict[str, Any]]:
    """The process groups on `host` that run in or name an owned directory (see REMOTE_SCAN)."""
    owned = [path for path in owned if path.startswith("/")]
    if not owned:
        return []
    code, out = run(ssh(host, "python3", "-I", "-"), 60, REMOTE_SCAN % json.dumps(list(owned)))
    if code:
        return []
    try:
        groups = json.loads(out)
    except ValueError:
        return []
    return [
        {
            "kind": "rig",
            "host": host,
            "pgid": g["pgid"],
            "pids": g["pids"],
            "age_seconds": int(g["age"]),
            "command": g["command"],
            "cwd": g["cwd"],
            "outputs": list(dict.fromkeys([*g["outputs"], *redirect_targets(g["command"])])),
        }
        for g in groups
    ]


def describe(jobs: Sequence[dict[str, Any]]) -> str:
    lines = []
    for job in jobs:
        where = f"{job['host']} process group {job['pgid']}"
        who = f"pids {', '.join(map(str, job['pids']))}" if job.get("pids") else f"local ssh pid {job['pid']}"
        age = f", running {job['age_seconds']} s" if job.get("age_seconds") else ""
        out = f"; output {', '.join(job['outputs'])}" if job.get("outputs") else ""
        lines.append(f"- {where} ({who}{age}): `{job['command'][:300]}`{out}")
    return "\n".join(lines)


def record(
    directory: Path,
    pids: Sequence[int],
    *,
    owns: Sequence[str] = (),
    proc: str = "/proc",
    run: Run = run_ssh,
) -> list[dict[str, Any]]:
    """Describe the lane's running rig jobs, write JOBS.json and add them to HANDOFF.md; returns the jobs.

    `pids` are the lane's local descendants (their ssh processes are jobs); every host those or the lane's transcript
    name is scanned for process groups in or naming one of `owns`."""
    local = census(pids, proc)
    hosts = list(dict.fromkeys([*(j["host"] for j in local), *hosts_in(directory / "transcript.jsonl")]))
    remote = [job for host in hosts for job in scan_host(host, owns, run)]
    # a local ssh that only waits on a remote group already listed is the same job
    jobs = [*remote, *(j for j in local if not any(j["command"] and j["command"] in r["command"] for r in remote))]
    (directory / JOBS_FILE).write_text(json.dumps({"schema": 1, "at": time.time(), "jobs": jobs}, indent=1) + "\n")
    if jobs:
        handoff = directory / "HANDOFF.md"
        old = handoff.read_text(errors="replace") if handoff.is_file() else ""
        old = old.split(HEADING)[0].rstrip("\n")
        handoff.write_text((old + "\n\n" if old else "") + f"{HEADING}\n\n{describe(jobs)}\n")
    return jobs


def alive(job: dict[str, Any], *, proc: str = "/proc", run: Run = run_ssh) -> bool:
    """The job still runs: a rig process group that still has members, or the local ssh process with the same start
    tick. A rig that cannot be asked counts as running, so the successor checks."""
    if job.get("kind") == "rig":
        listing = f"ps -eo pid=,pgid= | awk -v g={int(job['pgid'])} '$2 == g {{print $1}}'"
        code, out = run(ssh(job["host"], listing), 20, "")
        return code == 255 or bool(out.split())
    try:
        return _stat_fields(job["pid"], proc)[19] == job["start"]
    except (OSError, IndexError):
        return False


def adopt_text(directory: Path, *, proc: str = "/proc", run: Run = run_ssh) -> str:
    """The brief paragraph for a successor: the closed lane's jobs that still run. Empty when there are none."""
    try:
        document = json.loads((directory / JOBS_FILE).read_text())
        jobs = [job for job in document["jobs"] if alive(job, proc=proc, run=run)]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not jobs:
        return ""
    return (
        "Jobs the closed lane left running (adopt, don't restart). These rig jobs are still alive; wait on them and "
        "read their output instead of starting the same work again. Start one again only when its output shows it "
        f"failed or ended without a result:\n{describe(jobs)}"
    )
