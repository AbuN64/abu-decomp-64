import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location("deploy_rig", Path(__file__).parent.parent / "ci" / "deploy_rig.py")
deploy_rig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_rig)

SHA = "a" * 12 + "b" * 28
NEW = f"/home/abu/venv-decomp-stage.{SHA[:12]}"
OLD = "/home/abu/venv-decomp-stage.0123456789ab"
ENVIRON = {deploy_rig.ENV: SHA}


class Seams:
    """A fake rig and fake git: the rig keeps the two links, `decomp tools deploy` moves the stage link."""

    def __init__(self, gitea=True, github=True, links=(OLD, OLD), deploy_code=0, readback_code=0, repoint_code=0):
        self.on = {"gitea": gitea, "github": github}
        self.links = dict(zip(deploy_rig.LINKS, links, strict=True))
        self.deploy_code, self.readback_code, self.repoint_code = deploy_code, readback_code, repoint_code
        self.ssh_calls, self.git_calls = [], []
        self.fetched = None

    def git(self, *args):
        self.git_calls.append(args)
        if "fetch" in args:
            self.fetched = next(name for name, url in deploy_rig.REMOTES.items() if url in args)
            return 0, ""
        if "merge-base" in args:
            return (0 if self.on[self.fetched] else 1), ""
        return 0, ""

    def ssh(self, command):
        self.ssh_calls.append(command)
        if command.startswith("readlink"):
            return 0, "\n".join(self.links.values())
        if "decomp tools deploy" in command:
            if not self.deploy_code:
                self.links["venv-decomp-stage"] = NEW
            return self.deploy_code, "engine said no" if self.deploy_code else "deployed"
        if "mv -T" in command:
            if not self.repoint_code:
                self.links["venv-decomp"] = NEW
            return self.repoint_code, ""
        if "make -k -j8 check" in command:
            if self.readback_code:
                return self.readback_code, "readback: make check failed at RageWars abc:\nboom"
            return 0, f"readback: make check exit 0 at RageWars gitea main {'c' * 40}"
        raise AssertionError(command)

    def commands(self, needle):
        return [c for c in self.ssh_calls if needle in c]


def run_main(seams, environ):
    saved = deploy_rig.ssh, deploy_rig.git
    deploy_rig.ssh, deploy_rig.git = seams.ssh, seams.git
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy_rig.main(environ)
    finally:
        deploy_rig.ssh, deploy_rig.git = saved
    return code, out.getvalue(), err.getvalue()


class RefusalTests(unittest.TestCase):
    def test_a_missing_or_malformed_sha_exits_1_before_any_call(self):
        for environ in ({}, {deploy_rig.ENV: "main"}, {deploy_rig.ENV: SHA[:12]}):
            seams = Seams()
            code, _, err = run_main(seams, environ)
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertEqual((seams.git_calls, seams.ssh_calls), ([], []))

    def test_a_sha_not_on_either_main_exits_1_with_one_line_and_leaves_the_rig_alone(self):
        for missing in ("gitea", "github"):
            seams = Seams(**{missing: False})
            code, out, err = run_main(seams, ENVIRON)
            self.assertEqual(code, 1)
            self.assertEqual(err.strip(), f"deploy refused: {SHA[:12]} is not on {missing} main")
            self.assertEqual((out, seams.ssh_calls), ("", []))

    def test_an_unreachable_remote_is_a_refusal_not_a_pass(self):
        seams = Seams()
        seams.git = lambda *args: (128, "fatal: unable to access\nfatal: no route") if "fetch" in args else (0, "")
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("cannot fetch gitea main", err)
        self.assertEqual(len(err.strip().splitlines()), 1)


class DeployTests(unittest.TestCase):
    def test_a_new_sha_deploys_through_the_slot_with_no_overrides_and_both_links_move(self):
        seams = Seams()
        code, out, err = run_main(seams, ENVIRON)
        self.assertEqual((code, err), (0, ""))
        (command,) = seams.commands("decomp tools deploy")
        self.assertEqual(command, f"~/finish/slot decomp tools deploy --sha {SHA} --venv ~/venv-decomp-stage")
        for flag in ("--unbake", "--recipe", "--taste", "--project-sha"):
            self.assertNotIn(flag, command)
        self.assertEqual(set(seams.links.values()), {NEW})
        self.assertIn(NEW, out)
        self.assertEqual(len(seams.commands("make -k -j8 check")), 1)

    def test_the_readback_worktree_is_gitea_main_run_in_a_slot_and_no_venv_is_removed(self):
        seams = Seams()
        run_main(seams, ENVIRON)
        (readback,) = seams.commands("make -k -j8 check")
        self.assertTrue(readback.startswith("~/finish/slot bash -c "))
        self.assertIn("fetch -q gitea main", readback)
        self.assertIn("unbake setup --no-make", readback)
        for command in seams.ssh_calls:
            if "venv" in command and "make -k" not in command:
                self.assertNotIn("rm ", command)

    def test_the_engine_failing_exits_1_and_skips_the_repoint_and_readback(self):
        seams = Seams(deploy_code=3)
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "deploy refused: decomp tools deploy exit 3: engine said no")
        self.assertEqual(seams.commands("mv -T") + seams.commands("make -k"), [])

    def test_a_failed_repoint_exits_1(self):
        code, _, err = run_main(Seams(repoint_code=1), ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("cannot repoint ~/venv-decomp", err)


class IdempotenceTests(unittest.TestCase):
    def test_links_already_at_the_sha_skip_the_install_and_still_run_the_readback(self):
        seams = Seams(links=(NEW, NEW))
        code, out, err = run_main(seams, ENVIRON)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("deploy skipped", out)
        self.assertEqual(seams.commands("decomp tools deploy") + seams.commands("mv -T"), [])
        self.assertEqual(len(seams.commands("make -k -j8 check")), 1)

    def test_only_the_decomp_link_behind_is_repointed_without_a_second_install(self):
        seams = Seams(links=(OLD, NEW))
        code, _, _ = run_main(seams, ENVIRON)
        self.assertEqual(code, 0)
        self.assertEqual(seams.commands("decomp tools deploy"), [])
        self.assertEqual(len(seams.commands("mv -T")), 1)
        self.assertEqual(set(seams.links.values()), {NEW})

    def test_a_second_run_after_a_deploy_is_a_skip(self):
        seams = Seams()
        run_main(seams, ENVIRON)
        seams.ssh_calls.clear()
        code, out, _ = run_main(seams, ENVIRON)
        self.assertEqual(code, 0)
        self.assertIn("deploy skipped", out)
        self.assertEqual(seams.commands("decomp tools deploy"), [])


class ReadbackTests(unittest.TestCase):
    def test_make_check_failing_exits_1_with_the_first_line_of_the_cause(self):
        for links in ((OLD, OLD), (NEW, NEW)):
            code, _, err = run_main(Seams(links=links, readback_code=1), ENVIRON)
            self.assertEqual(code, 1)
            self.assertEqual(err.strip(), "deploy refused: readback: make check failed at RageWars abc:")

    def test_a_link_that_does_not_read_back_to_the_new_directory_exits_1_before_make(self):
        seams = Seams()
        original = seams.ssh

        def drifting(command):
            code, out = original(command)
            if "mv -T" in command:
                seams.links["venv-decomp"] = OLD
            return code, out

        seams.ssh = drifting
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("readback:", err)
        self.assertIn("venv-decomp", err)
        self.assertEqual(seams.commands("make -k"), [])

    def test_unreadable_links_exit_1(self):
        seams = Seams()
        seams.ssh = lambda command: (255, "ssh: connect to host 192.168.0.101 port 22: No route to host")
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("cannot read the rig venv links", err)


class DeployFileTests(unittest.TestCase):
    """ButlerMaster executes `.deploy` as one repository-relative executable path, not as a shell line."""

    def test_the_deploy_file_names_one_existing_executable_script_with_no_spaces(self):
        root = Path(__file__).parent.parent
        lines = (root / ".deploy").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        path = lines[0]
        self.assertEqual(path, path.strip())
        self.assertNotRegex(path, r"\s")
        self.assertFalse(path.startswith("/"))
        self.assertTrue((root / path).is_file())
        self.assertTrue(os.access(root / path, os.X_OK))
        self.assertEqual((root / path).read_text().splitlines()[0], "#!/usr/bin/env python3")


if __name__ == "__main__":
    unittest.main()
