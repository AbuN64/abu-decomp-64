import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import unittest

from abu_decomp.supervisor.gdb import Bridge

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as name:
            return name.read().strip()
    except OSError:
        return ""


class OwnedChildren(unittest.TestCase):
    def test_sigkilled_owner_leaves_no_survivor_within_two_seconds(self):
        # owner -> bridge (owned) -> gdb stand-in (owned) -> game stand-in (setpriv wrapped, as gdb's exec-wrapper does)
        bridge = textwrap.dedent(
            """
            import subprocess, sys
            from abu_decomp import procguard
            game = procguard.wrap(["sleep", "300"]) if sys.argv[1] == "wrap" else ["sleep", "300"]
            cmd = " ".join(game) + " & echo $! ; wait"
            gdb = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, text=True,
                                   **procguard.owned())
            print(gdb.pid, gdb.stdout.readline().strip(), flush=True)
            gdb.wait()
            """
        )
        owner = textwrap.dedent(
            f"""
            import subprocess, sys
            from abu_decomp import procguard
            child = subprocess.Popen([sys.executable, "-c", {bridge!r}, sys.argv[1]], stdout=subprocess.PIPE, text=True,
                                     **procguard.owned())
            print(child.pid, child.stdout.readline().strip(), flush=True)
            child.wait()
            """
        )
        env = {**os.environ, "PYTHONPATH": SRC}
        mode = "wrap" if shutil.which("setpriv") else "plain"
        owner_proc = subprocess.Popen([sys.executable, "-c", owner, mode], stdout=subprocess.PIPE, text=True, env=env)
        try:
            bridge_pid, gdb_pid, game_pid = map(int, owner_proc.stdout.readline().split())
            pids = [bridge_pid, gdb_pid, game_pid]
            self.assertTrue(all(alive(p) for p in pids))
            # the game pid is printed at the fork, before setpriv arms its death signal and execs: kill the owner only
            # once the game runs, or a loaded host killed it first and the game outlived an unarmed setpriv
            armed = time.monotonic() + 10
            while time.monotonic() < armed and comm(game_pid) not in ("sleep", ""):
                time.sleep(0.01)
            os.kill(owner_proc.pid, signal.SIGKILL)
            owner_proc.wait()
            end = time.monotonic() + 2
            while time.monotonic() < end and any(alive(p) for p in pids):
                time.sleep(0.02)
            self.assertEqual([p for p in pids if alive(p)], [])
        finally:
            owner_proc.kill()

    def test_gdb_bridge_wraps_the_game_it_launches(self):
        args = Bridge.wrapper_args()
        if shutil.which("setpriv"):
            self.assertEqual(args, ["-iex", "set exec-wrapper setpriv --pdeathsig kill"])
        else:
            self.assertEqual(args, [])


if __name__ == "__main__":
    unittest.main()
