import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from abu_decomp.config import load_profile
from abu_decomp.supervisor import SupervisorProfile
from abu_decomp.supervisor.supervisor import Supervisor

SRC = str(Path(__file__).resolve().parents[1] / "src")


class HeadlessSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        release = self.root / "runtime/releases/port/abc1234"
        release.mkdir(parents=True)
        self.binary = release / "game"
        self.binary.write_text("#!/bin/sh\necho boot-step: Init\nexit ${GAME_EXIT:-3}\n")
        self.binary.chmod(0o755)
        (release.parent / "current").symlink_to("abc1234")
        self.feed = self.root / "narration.jsonl"
        self.role = {
            "kind": "supervised",
            "release_current": str(release.parent / "current"),
            "command": ["{release}/game"],
            "status_path": "status/port.json",
            "runs_root": "runs",
            "step_prefixes": ["boot-step:"],
            "retry_delay_seconds": 30,
            "watchdog_seconds": 20,
            "auto_retry": False,
        }
        self.seat = self.root / "seat.json"
        self.write({"port": self.role})

    def write(self, roles, events=True):
        profile = {"schema_version": 1, "name": "seat", "supervisor": {"runtime_root": "runtime", "roles": roles}}
        if events:
            profile["events"] = {"path": str(self.feed)}
        self.seat.write_text(json.dumps(profile))

    def test_a_failed_run_writes_status_receipt_and_one_run_fail_event(self):
        seat = SupervisorProfile(load_profile(self.seat))
        supervisor = Supervisor(seat, "port", seat.role("port", "supervised"))

        async def scenario():
            supervisor.runs_root.mkdir(parents=True)
            task = asyncio.create_task(supervisor.run_once())
            while supervisor.phase != "held_until_restart":
                await asyncio.sleep(0.02)
            supervisor.restart.set()
            return await asyncio.wait_for(task, 5)

        self.assertFalse(asyncio.run(scenario()))
        status = json.loads((self.root / "status/port.json").read_text())
        self.assertEqual((status["last_result"], status["exit_code"], status["step"]), ("nonzero_exit", 3, "Init"))
        self.assertIsNone(status["terminal_rows_columns"])
        event = json.loads(self.feed.read_text())
        self.assertEqual(
            (event["schema"], event["kind"], event["actor"], event["status"]), (2, "RUN-FAIL", "port", "failed")
        )
        self.assertEqual(event["payload"]["outcome"], "nonzero_exit")
        self.assertTrue(Path(event["evidence"]).name == "receipt.json" and Path(event["evidence"]).is_file())

    def test_a_seat_without_events_records_no_event(self):
        self.write({"port": self.role}, events=False)
        seat = SupervisorProfile(load_profile(self.seat))
        self.assertIsNone(seat.events_path)

    def test_role_kind_and_section_keys_are_checked(self):
        with self.assertRaises(Exception) as caught:
            SupervisorProfile(load_profile(self.seat)).role("port", "artifact")
        self.assertEqual(caught.exception.key, "supervisor.role")
        self.seat.write_text(json.dumps({"schema_version": 1, "name": "s", "supervisor": {"roles": {}}}))
        with self.assertRaises(Exception) as caught:
            SupervisorProfile(load_profile(self.seat))
        self.assertIn("runtime_root", caught.exception.detail)

    def test_run_is_headless_and_answers_sighup_and_sigterm(self):
        env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
        process = subprocess.Popen(
            [sys.executable, "-m", "abu_decomp", "--profile", str(self.seat), "supervisor", "run", "--role", "port"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )  # fmt: skip
        self.addCleanup(lambda: process.poll() is None and process.kill())
        status = self.root / "status/port.json"

        def wait_for(attempt):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if status.is_file():
                    value = json.loads(status.read_text())
                    if value["attempt"] == attempt and value["phase"] == "held_until_restart":
                        return value
                time.sleep(0.05)
            self.fail(f"attempt {attempt} never held: {status.read_text() if status.is_file() else None}")

        wait_for(1)
        # the published pidfile names the process; SIGHUP restarts the held run
        from abu_decomp.supervisor.supervisor import pidfile

        self.assertEqual(int(pidfile(status).read_text()), process.pid)
        process.send_signal(signal.SIGHUP)
        wait_for(2)
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        envelope = json.loads(output)
        self.assertEqual(envelope["result"], {"closed": True, "role": "port", "status_path": str(status)})
        self.assertFalse(pidfile(status).exists())
        # one RUN-FAIL per distinct failure: the same failure twice is announced once
        self.assertEqual(len(self.feed.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
