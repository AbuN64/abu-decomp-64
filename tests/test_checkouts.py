import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import deploy, tools
from abu_decomp.config import load_profile

URL = "https://github.com/AbuN64/cake-taste-64"


def git(path, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "f").write_text("1")
        git(self.origin, "add", "f")
        git(self.origin, "commit", "-q", "-m", "one")
        self.old = git(self.origin, "rev-parse", "HEAD")
        (self.origin / "f").write_text("2")
        git(self.origin, "commit", "-q", "-am", "two")
        self.new = git(self.origin, "rev-parse", "HEAD")
        self.copy = self.root / "taste"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.copy)], check=True)
        git(self.copy, "checkout", "-q", "--detach", self.old)
        (self.root / "project").mkdir()
        (self.root / "host.toml").write_text("")
        table = {
            "project": "project",
            "host": "host.toml",
            "timeout_seconds": 5,
            "checkouts": {"taste": str(self.copy)},
        }
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "s", "tools": table}))
        self.profile = load_profile(self.root / "seat.json")
        self.pin(self.new)

    def pin(self, commit):
        pins = {name: tools.Pin(tool.distribution, URL, commit) for name, tool in tools.TOOLS.items()}
        patcher = mock.patch("abu_decomp.tools.pins", return_value=pins)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_checkout_off_the_pin_is_refused_naming_path_head_and_pin(self):
        with self.assertRaises(tools.DependencyError) as caught:
            tools.require_checkouts(self.profile, ["taste"])
        self.assertEqual(caught.exception.key, "tools.checkout_drift")
        for part in (str(self.copy), self.old, self.new):
            self.assertIn(part, caught.exception.detail)

    def test_other_tools_and_agreeing_checkouts_pass(self):
        tools.require_checkouts(self.profile, ["unbake", "recipe"])
        git(self.copy, "checkout", "-q", "--detach", self.new)
        tools.require_checkouts(self.profile, ["taste"])

    def test_invoke_fails_before_running_the_tool(self):
        real = subprocess.run
        calls = []

        def spy(argv, *args, **kwargs):
            calls.append(argv[0])
            return real(argv, *args, **kwargs)

        with mock.patch("abu_decomp.tools.subprocess.run", spy), self.assertRaises(tools.DependencyError) as caught:
            tools.invoke("taste", self.profile, ["repro", "probe"])
        self.assertEqual(caught.exception.key, "tools.checkout_drift")
        self.assertEqual(set(calls), {"git"})  # only the HEAD lookup ran, never the tool

    def test_unknown_checkout_key_is_a_schema_error(self):
        text = (self.root / "seat.json").read_text().replace('"taste"', '"nope"')
        (self.root / "seat.json").write_text(text)
        with self.assertRaises(tools.DependencyError):
            tools.checkouts_of(load_profile(self.root / "seat.json"))

    def requirements(self):
        return [{"name": "cake-taste-64", "url": URL, "commit": self.new}]

    def test_deploy_aligns_a_clean_checkout_to_the_pin(self):
        wanted, refused = deploy.checkout_steps(self.profile, self.requirements())
        self.assertEqual(refused, [])
        done, findings = deploy.align_checkouts(wanted, deploy.run_command)
        self.assertEqual((findings, done[0]["ok"]), ([], True))
        self.assertEqual(tools.checkout_head(self.copy), self.new)
        self.assertEqual(tools.checkout_drift(self.profile), [])

    def test_deploy_refuses_a_checkout_with_local_changes_and_leaves_it(self):
        (self.copy / "f").write_text("mine")
        _, refused = deploy.checkout_steps(self.profile, self.requirements())
        self.assertEqual([f["key"] for f in refused], ["deploy.checkout_dirty"])
        self.assertEqual((self.copy / "f").read_text(), "mine")
        self.assertEqual(tools.checkout_head(self.copy), self.old)

    def test_deploy_runs_nothing_when_a_checkout_is_dirty_and_installs_after_aligning(self):
        calls = []

        def run(argv, cwd=None):
            calls.append(list(argv))
            return deploy.run_command(argv, cwd) if argv[0] == "git" else (0, "ok")

        real = self.root / "venv.0"
        (real / "bin").mkdir(parents=True)
        (real / "bin" / "python").write_text("")
        venv = self.root / "venv"
        venv.symlink_to(real)
        with mock.patch("abu_decomp.deploy.plan", return_value=(self.requirements(), [])):
            (self.copy / "f").write_text("mine")
            _, code, findings = deploy.deploy(self.profile, "a" * 40, [venv], {}, run=run)
            self.assertEqual((code, findings[0]["key"], calls), (1, "deploy.checkout_dirty", []))
            (self.copy / "f").write_text("2")
            git(self.copy, "checkout", "-q", "--", "f")
            git(self.copy, "reset", "-q", "--hard", self.old)
            result, code, findings = deploy.deploy(self.profile, "a" * 40, [venv], {}, run=run)
        self.assertEqual(code, 1 if any(f["key"] == "deploy.smoke" for f in findings) else 0)
        self.assertEqual(tools.checkout_head(self.copy), self.new)
        self.assertEqual(result["checkouts"][0]["ok"], True)
        self.assertTrue(any("pip" in " ".join(c) or "install" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
