import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from abu_decomp.lanes import continuation, jobs, relaunch


class FakeProc:
    """A /proc tree with ssh and other processes."""

    def __init__(self, root: Path):
        self.root = root

    def add(self, pid, argv, pgid=None, start=1000, outputs=None):
        directory = self.root / str(pid)
        (directory / "fd").mkdir(parents=True)
        (directory / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        fields = ["S", "1", str(pgid or pid)] + ["0"] * 16 + [str(start)]
        (directory / "stat").write_text(f"{pid} (ssh) " + " ".join(fields) + "\n")
        for number, target in (outputs or {}).items():
            os.symlink(target, directory / "fd" / str(number))


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = FakeProc(self.root / "proc")
        self.proc.root.mkdir()
        self.asked: list[list[str]] = []
        self.group = {
            "pgid": 777,
            "pids": [777, 778],
            "command": "python3 score.py > /home/abu/lanes/L/out.log 2>&1",
            "cwd": "/home/abu/lanes/L",
            "age": 41.5,
            "outputs": ["/home/abu/lanes/L/err.log"],
        }
        self.members = "777\n"

    def run_ssh(self, argv, timeout, stdin=""):
        self.asked.append([*argv, stdin])
        if any("pgid=" in a for a in argv):
            return 0, self.members
        return 0, json.dumps([self.group])

    def test_split_ssh_and_redirect_targets(self):
        argv = ["ssh", "-o", "BatchMode=yes", "-p", "22", "abu@rig", "score", "600"]
        self.assertEqual(jobs.split_ssh(argv), ("abu@rig", "score 600"))
        self.assertEqual(jobs.split_ssh(["bash", "-c", "x"]), ("", ""))
        self.assertEqual(
            jobs.redirect_targets("cd x && python score.py > /home/abu/out.log 2>&1; make | tee -a /home/abu/m.log"),
            ["/home/abu/out.log", "/home/abu/m.log"],
        )
        transcript = self.root / "transcript.jsonl"
        transcript.write_text('{"value": "ssh -o BatchMode=yes abu@192.168.0.101 \\"cd x && make\\""}\n')
        self.assertEqual(jobs.hosts_in(transcript), ["abu@192.168.0.101"])

    def test_census_keeps_only_ssh_jobs_with_their_group_host_and_outputs(self):
        command = "python score.py > /home/abu/state/s.log 2>&1"
        self.proc.add(10, ["ssh", "abu@rig", command], pgid=9, outputs={1: "/work/local.log"})
        self.proc.add(11, ["bash", "-c", "work 1"])
        found = jobs.census([10, 11, 99], str(self.proc.root))
        self.assertEqual(len(found), 1)
        job = found[0]
        self.assertEqual((job["pid"], job["pgid"], job["host"]), (10, 9, "abu@rig"))
        self.assertEqual(job["outputs"], ["/work/local.log", "/home/abu/state/s.log"])

    def test_the_remote_scan_finds_process_groups_in_owned_directories_only(self):
        # the scan script itself, run for real on a tree of fake owned processes
        owned = self.root / "owned"
        owned.mkdir()
        log = owned / "job.log"
        output = log.open("w")
        self.addCleanup(output.close)
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; print('x', flush=True); time.sleep(30)  # {owned}"],
            cwd=owned,
            stdout=output,
            start_new_session=True,
        )
        elsewhere = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        for process in (child, elsewhere):
            self.addCleanup(lambda p=process: (p.kill(), p.wait()))
        done = subprocess.run(
            [sys.executable, "-I", "-"],
            input=jobs.REMOTE_SCAN % json.dumps([str(owned)]),
            capture_output=True,
            text=True,
            check=True,
        )
        groups = json.loads(done.stdout)
        self.assertEqual([g["pgid"] for g in groups], [child.pid])
        self.assertEqual(groups[0]["outputs"], [str(log)])
        self.assertEqual(groups[0]["cwd"], str(owned))

    def test_closing_records_the_jobs_in_json_and_handoff_and_the_successor_adopts_them(self):
        directory = self.root / "L-1"
        directory.mkdir()
        (directory / "HANDOFF.md").write_text("state: scoring\n")
        (directory / "transcript.jsonl").write_text('ssh abu@rig "cd /home/abu/lanes/L && nohup python3 score.py &"\n')
        found = jobs.record(
            directory, [], owns=["/home/abu/lanes/L", "relative"], proc=str(self.proc.root), run=self.run_ssh
        )
        self.assertEqual([(j["kind"], j["host"], j["pgid"]) for j in found], [("rig", "abu@rig", 777)])
        self.assertIn('["/home/abu/lanes/L"]', self.asked[0][-1])  # only the absolute owned path is asked about
        handoff = (directory / "HANDOFF.md").read_text()
        self.assertTrue(handoff.startswith("state: scoring"))
        self.assertIn(jobs.HEADING, handoff)
        self.assertIn("process group 777", handoff)
        self.assertIn("/home/abu/lanes/L/out.log", handoff)
        self.assertEqual(json.loads((directory / jobs.JOBS_FILE).read_text())["jobs"][0]["host"], "abu@rig")
        # recorded again by the next link: the section is replaced, not stacked
        jobs.record(directory, [], owns=["/home/abu/lanes/L"], proc=str(self.proc.root), run=self.run_ssh)
        self.assertEqual((directory / "HANDOFF.md").read_text().count(jobs.HEADING), 1)
        # the group still has members: the brief says adopt, ahead of the rest of the continuation
        text = jobs.adopt_text(directory, run=self.run_ssh)
        self.assertIn("adopt, don't restart", text)
        self.assertIn("score.py", text)
        brief = continuation.continuation_brief("TASK\n", "LANE RULES\n", "L-1", directory, self.root / "L-2", text)
        section = brief.split(continuation.HEADING)[1]
        self.assertLess(section.index("adopt, don't restart"), section.index("This lane continues"))
        # the group has ended: no paragraph
        self.members = ""
        self.assertEqual(jobs.adopt_text(directory, run=self.run_ssh), "")

    def test_a_link_waiting_on_its_own_job_is_not_ended_and_spends_no_budget(self):
        self.assertEqual(relaunch.plan("result", 2, "", False, False, False)[0], "none")
        action, why = relaunch.plan("result", 0, "", False, False, False, "", waiting=2)
        self.assertEqual(action, "continue")
        self.assertIn("2 rig job", why)
        self.assertEqual(relaunch.next_budget(3, "", waiting=2), 3)
        self.assertEqual(relaunch.next_budget(3, ""), 2)
        self.assertEqual(relaunch.plan("result", 2, "DONE\nall", True, True, False, "", waiting=1)[0], "end")
        self.assertEqual(relaunch.plan("result", 2, "", False, False, True, "", waiting=1)[0], "none")


if __name__ == "__main__":
    unittest.main()
