"""Ships that queue together land together (10-22: five ships deep, the head waited 1000 s, each ship ~62 s of gates).

A `--patch` ship publishes its inputs while it waits for its ticket (`decomp-ship.inputs/<ticket>.json`, in git's common
dir beside the ticket queue). The ship that takes the lock adopts every input waiting behind it (an atomic rename to
`<ticket>.claimed-<its pid>`), lands them all in the one round `land` already runs (one setup, one check, one sync, one
audit; one commit per input, a build one input breaks is bisected and only that input refused) and writes each waiting
ship its own result (`decomp-ship.done/<ticket>.json`). A waiting ship that finds its input claimed stops queueing for
the lock and waits for that result; if the adopter dies without one the input goes back and the ship queues as before.
Provenance is per input: the commit plan, the inputs rows and the findings keep their lane."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

BATCH_MAX = 8  # inputs adopted into one round, the adopter's own included
VERSION = 2  # a ship adopts only inputs published by ships that run the same gates (v2: placeholder names refused)
STALE = 3600.0  # seconds a result nobody collected stays


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Batch:
    """One ship's side of the shared input folders (`common` is git's common dir)."""

    def __init__(self, common: Path, ticket: str) -> None:
        self.inputs, self.done, self.ticket = common / "decomp-ship.inputs", common / "decomp-ship.done", ticket
        self.inputs.mkdir(exist_ok=True)
        self.done.mkdir(exist_ok=True)
        self.mine = self.inputs / f"{ticket}.json"
        self.adopted: list[tuple[str, Path]] = []  # (ticket, claimed file) of the inputs this ship took
        self.messages: dict[str, str] = {}  # the commit messages those inputs came with

    def publish(self, patches: list[tuple[str, Path]], messages: dict[str, str]) -> None:
        body = {
            "v": VERSION,
            "pid": os.getpid(),
            "patches": [[lane, str(path)] for lane, path in patches],
            "messages": messages,
        }
        temp = self.inputs / f".{self.ticket}.tmp"
        temp.write_text(json.dumps(body))
        temp.rename(self.mine)

    def withdraw(self) -> None:
        self.mine.unlink(missing_ok=True)

    def poll(self) -> tuple[str, Any]:
        """('done', result) once an adopter landed this ship's input, ('claimed', pid) while one holds it (a dead
        adopter's claim goes back to the queue), else ('queued', None)."""
        found = self.result()
        if found is not None:
            return "done", found
        for claimed in self.inputs.glob(f"{self.ticket}.claimed-*"):
            pid = int(claimed.name.rpartition("-")[2])
            if alive(pid):
                return "claimed", pid
            claimed.rename(self.mine)  # the adopter died without a result: queue again
        return "queued", None

    def result(self) -> Any:
        try:
            return json.loads((self.done / f"{self.ticket}.json").read_text())
        except (OSError, ValueError):
            return None

    def adopt(self, taken: set[str]) -> list[tuple[str, Path]]:
        """Take the live, intact inputs waiting behind this ship, oldest first; the lanes they add come back as
        (lane, patch) rows and their messages as `self.messages`."""
        rows: list[tuple[str, Path]] = []
        for path in sorted(self.inputs.glob("*.json")):
            if path == self.mine or len(self.adopted) + 1 >= BATCH_MAX:
                continue
            try:
                body = json.loads(path.read_text())
                lanes = [(lane, Path(file)) for lane, file in body["patches"]]
                pid = int(body["pid"])
                if body.get("v") != VERSION:
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not alive(pid) or not lanes or any(lane in taken for lane, _ in lanes):
                continue
            if any(not file.is_file() for _, file in lanes):
                continue
            claimed = path.with_name(f"{path.stem}.claimed-{os.getpid()}")
            try:
                path.rename(claimed)
            except OSError:
                continue
            taken.update(lane for lane, _ in lanes)
            self.adopted.append((path.stem, claimed))
            self.messages.update(body.get("messages") or {})
            rows += lanes
        return rows

    def lanes_of(self, claimed: Path) -> list[str]:
        return [lane for lane, _ in json.loads(claimed.read_text())["patches"]]

    def release(self, outcome: tuple[dict[str, Any], int, list[dict[str, Any]]] | None) -> None:
        """Give each adopted input its result (a restriction of `outcome` to its lanes); with no outcome the claims go
        back to the queue so those ships land their inputs themselves."""
        for ticket, claimed in self.adopted:
            try:
                lanes = set(self.lanes_of(claimed))
            except (OSError, ValueError):
                continue
            if outcome is None:
                claimed.rename(self.inputs / f"{ticket}.json")
                continue
            temp = self.done / f".{ticket}.tmp"
            temp.write_text(json.dumps(restrict(outcome, lanes, adopter=os.getpid()), default=str))
            temp.rename(self.done / f"{ticket}.json")
            claimed.unlink(missing_ok=True)
        self.adopted = []
        cutoff = time.time() - STALE
        for old in self.done.glob("*.json"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)


def restrict(
    outcome: tuple[dict[str, Any], int, list[dict[str, Any]]], lanes: set[str], adopter: int | None = None
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """The ship result as one ship of the round sees it: its own inputs, its own findings (findings of no lane are
    the round's and stay), its own verdict."""
    shipped, code, findings = outcome
    inputs = [row for row in shipped.get("inputs", []) if row["lane"] in lanes]
    names = sorted({name for row in inputs for name in row.get("functions", [])})
    own = [f for f in findings if not f.get("lane") or f["lane"] in lanes]
    lost = [row for row in inputs if row["status"] == "refused"]
    mine = {
        **shipped,
        "lane": ", ".join(row["lane"] for row in inputs),
        "inputs": inputs,
        "functions": names,
        "batch": {"lanes": [row["lane"] for row in shipped.get("inputs", [])], "adopter": adopter},
    }
    if shipped.get("pushed") and len(lost) == len(inputs) and inputs:  # the round pushed, this ship's inputs did not
        mine["pushed"], mine["refused"] = None, {"key": lost[0].get("key"), "message": lost[0].get("message")}
        code = 1
    elif shipped.get("pushed"):
        code = 0
    return mine, code, own
