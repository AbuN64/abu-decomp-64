"""Ships that queue together land together: the ship that takes the lock lands the inputs waiting behind it."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import test_ship

from abu_decomp.ship import ShipError, ship
from abu_decomp.ship_batch import Batch, restrict


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        shared = test_ship.ShipTests()  # the same remote, warm clone and profile the ship tests use
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        self.__dict__.update({k: v for k, v in shared.__dict__.items() if k in {"root", "remote", "tree", "profile"}})

    def remote_tip(self) -> str:
        return test_ship.git(self.remote, "rev-parse", "main").strip()

    def multi(self, calls: list[list[str]], refuse: str = "") -> Any:
        def fake(profile: Any, tree: Path, base: str, patches: Any, **_: Any) -> Any:
            calls.append([lane for lane, _ in patches])
            plans, lanes = [], []
            for lane, _ in patches:
                (tree / f"{lane}.c").write_text(f"int {lane};\n")
                plans.append({"lane": lane, "files": [f"{lane}.c"], "functions": [lane], "body": None})
                plans[-1]["message"] = f"Match {lane}"
                lanes.append({"lane": lane, "functions": {"names": [lane]}, "status": "applied"})
            return {"pass": True, "commits": plans, "lanes": lanes}, 0, []

        return fake

    def test_two_queued_ships_land_in_one_round_and_each_gets_its_own_result(self) -> None:
        calls: list[list[str]] = []
        common = self.tree / ".git"
        held = os.open(common / "decomp-ship.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(held, fcntl.LOCK_EX)
        out: dict[str, Any] = {}

        def run(lane: str) -> None:
            append = lambda path, record: {"ok": True}  # noqa: E731
            out[lane] = ship(
                self.profile, self.tree, [(lane, self.root / f"{lane}.patch")], landing=self.multi(calls), append=append
            )

        threads = [threading.Thread(target=run, args=(lane,)) for lane in ("L1", "L2")]
        for thread in threads:
            thread.start()
            deadline = time.time() + 20
            while len(list((common / "decomp-ship.inputs").glob("*.json"))) < threads.index(thread) + 1:
                self.assertLess(time.time(), deadline)
                time.sleep(0.05)
        (self.root / "L1.patch").write_text("")
        (self.root / "L2.patch").write_text("")
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        for thread in threads:
            thread.join(30)
        self.assertEqual(calls, [["L1", "L2"]])
        for lane in ("L1", "L2"):
            shipped, code, _ = out[lane]
            self.assertEqual(code, 0)
            self.assertEqual([row["lane"] for row in shipped["inputs"]], [lane])
            self.assertEqual(shipped["functions"], [lane])
            self.assertEqual(shipped["pushed"], self.remote_tip())
        self.assertEqual(
            test_ship.git(self.remote, "log", "--format=%s", "-2", "main").split(), ["Match", "L2", "Match", "L1"]
        )
        self.assertEqual(list((common / "decomp-ship.inputs").glob("*")), [])

    def test_a_claim_of_a_dead_adopter_goes_back_to_the_queue(self) -> None:
        common = self.tree / ".git"
        common.mkdir(exist_ok=True)
        batch = Batch(common, "00000000000000000002-1")
        batch.publish([("L", self.root / "l.patch")], {})
        batch.mine.rename(batch.inputs / "00000000000000000002-1.claimed-999999999")
        self.assertEqual(batch.poll(), ("queued", None))
        self.assertTrue(batch.mine.exists())

    def test_the_round_verdict_is_split_per_input(self) -> None:
        shipped = {
            "pushed": "abc",
            "inputs": [
                {"lane": "A", "status": "refused", "key": "land.check", "message": "no"},
                {"lane": "B", "status": "shipped", "functions": ["fb"]},
            ],
        }
        mine, code, found = restrict((shipped, 0, [{"key": "x", "lane": "A"}, {"key": "y", "lane": "B"}]), {"A"})
        self.assertEqual(
            (code, mine["pushed"], mine["refused"]["key"], [f["key"] for f in found]), (1, None, "land.check", ["x"])
        )
        mine, code, _ = restrict((shipped, 0, []), {"B"})
        self.assertEqual((code, mine["pushed"], mine["functions"]), (0, "abc", ["fb"]))
        json.dumps(mine)


class PrecheckTests(unittest.TestCase):
    def test_a_long_subject_is_refused_before_the_queue_wait(self) -> None:
        shared = test_ship.ShipTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        held = os.open(shared.tree / ".git" / "decomp-ship.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)  # a ship ahead: a refusal after the wait would never come
        with self.assertRaises(ShipError) as raised:
            ship(shared.profile, shared.tree, [("L", shared.root / "l.patch")], messages={"L": "m" * 80}, lock_wait=1)
        self.assertEqual(raised.exception.key, "land.commit_subject")
        self.assertIn("decomp", raised.exception.next or "")
