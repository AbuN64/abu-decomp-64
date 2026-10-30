import importlib.util
import io
import os
import re
import site
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath

spec = importlib.util.spec_from_file_location("deploy_rig", Path(__file__).parent.parent / "ci" / "deploy_rig.py")
deploy_rig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_rig)

SHA = "a" * 12 + "b" * 28
NEW = f"/home/abu/venv-decomp-stage.{SHA[:12]}"
OLD = "/home/abu/venv-decomp-stage.0123456789ab"
ENVIRON = {deploy_rig.ENV: SHA}
CHECKOUT = "/home/abu/finish/DEPLOY-SUITE.AbCdEf/checkout"
SUMMARY = "1767 passed, 0 failed, 5 skipped in 204 s"


def suite_output(sha=SHA, imported=f"{CHECKOUT}/src/abu_decomp/__init__.py", failed=None):
    lines = [f"suite-checkout: {CHECKOUT}", f"suite-sha: {sha}", f"suite-import: {imported}"]
    if failed:
        lines += ["suite-summary: 1766 passed, 1 failed, 5 skipped in 204 s", f"suite-first-failure: {failed}"]
    else:
        lines.append(f"suite-summary: {SUMMARY}")
    return "\n".join([*lines, "suite-log: /home/abu/finish/deploy-suite-aaaaaaaaaaaa.log"])


class FakeLock:
    """The rig lock seam: records `lock` and `unlock` in `events` (shared with the fake rig to prove ordering)."""

    def __init__(self, events=None, notes=()):
        self.events = [] if events is None else events
        self.notes = list(notes)

    def __call__(self):
        return self

    def __enter__(self):
        self.events.append("lock")
        return list(self.notes)

    def __exit__(self, *exc):
        self.events.append("unlock")
        return False


NEWER = "d" * 12 + "e" * 28


class Seams:
    """A fake rig and fake git: the rig keeps the two links, `decomp tools deploy` moves the stage link."""

    def __init__(
        self,
        gitea=True,
        github=True,
        links=(OLD, OLD),
        deploy_code=0,
        readback_code=0,
        repoint_code=0,
        suite_code=0,
        suite_out=None,
        head=None,
        notes=(),
    ):
        self.head = head
        self.events = []
        self.lock = FakeLock(self.events, notes)
        self.on = {"gitea": gitea, "github": github}
        self.suite_code, self.suite_out = suite_code, suite_out
        self.links = dict(zip(deploy_rig.LINKS, links, strict=True))
        self.deploy_code, self.readback_code, self.repoint_code = deploy_code, readback_code, repoint_code
        self.ssh_calls, self.git_calls = [], []
        self.fetched = None

    def git(self, *args):
        self.git_calls.append(args)
        self.events.append("git")
        if "rev-parse" in args:
            return 0, self.head or ""
        if "fetch" in args:
            self.fetched = next(name for name, url in deploy_rig.REMOTES.items() if url in args)
            return 0, ""
        if "merge-base" in args:
            return (0 if self.on[self.fetched] else 1), ""
        return 0, ""

    def ssh(self, command):
        self.ssh_calls.append(command)
        self.events.append("ssh")
        if command.startswith("readlink"):
            return 0, "\n".join(self.links.values())
        if "decomp tools deploy" in command:
            if not self.deploy_code:
                self.links["venv-decomp-stage"] = f"/home/abu/venv-decomp-stage.{command.split('--sha ')[1][:12]}"
            return self.deploy_code, "engine said no" if self.deploy_code else "deployed"
        if "DEPLOY-SUITE" in command:
            if self.suite_out is not None:
                return self.suite_code, self.suite_out
            sha = re.search(r" suite ([0-9a-f]{40}) ", command).group(1)
            failed = "tests/test_x.py::Case::test_y" if self.suite_code else None
            return self.suite_code, suite_output(sha=sha, failed=failed)
        if "mv -T" in command:
            if not self.repoint_code:
                self.links["venv-decomp"] = re.search(r"ln -sfn (\S+)", command).group(1)
            return self.repoint_code, ""
        if "make -k -j8 check" in command:
            if self.readback_code:
                return self.readback_code, "readback: make check failed at RageWars abc:\nboom"
            return 0, f"readback: make check exit 0 at RageWars gitea main {'c' * 40}"
        raise AssertionError(command)

    def commands(self, needle):
        return [c for c in self.ssh_calls if needle in c]


def run_main(seams, environ):
    saved = deploy_rig.ssh, deploy_rig.git, deploy_rig.rig_lock
    deploy_rig.ssh, deploy_rig.git, deploy_rig.rig_lock = seams.ssh, seams.git, seams.lock
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy_rig.main(environ)
    finally:
        deploy_rig.ssh, deploy_rig.git, deploy_rig.rig_lock = saved
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
            if "venv" in command and "make -k" not in command and "DEPLOY-SUITE" not in command:
                self.assertNotIn("rm ", command)

    def test_the_suite_script_removes_only_its_own_scratch_checkout(self):
        removals = [line for line in deploy_rig.SUITE.splitlines() if "rm " in line]
        self.assertEqual(len(removals), 1)
        self.assertIn('rm -rf "$work"', removals[0])

    def test_the_engine_failing_exits_1_and_skips_the_repoint_and_readback(self):
        seams = Seams(deploy_code=3)
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "deploy refused: decomp tools deploy exit 3: engine said no")
        self.assertEqual(seams.commands("mv -T") + seams.commands("make -k") + seams.commands("DEPLOY-SUITE"), [])

    def test_a_failed_repoint_exits_1(self):
        code, _, err = run_main(Seams(repoint_code=1), ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("cannot repoint ~/venv-decomp", err)


class IdempotenceTests(unittest.TestCase):
    def test_links_already_at_the_sha_skip_the_install_and_still_run_the_readback(self):
        seams = Seams(links=(NEW, NEW))
        code, out, err = run_main(seams, ENVIRON)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("build and swap skipped", out)
        self.assertEqual(seams.commands("decomp tools deploy") + seams.commands("mv -T"), [])
        self.assertEqual(len(seams.commands("DEPLOY-SUITE")), 1)
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
        self.assertIn("build and swap skipped", out)
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


class SuiteGateTests(unittest.TestCase):
    """The full suite at exactly the landed SHA, after the build and before ~/venv-decomp moves."""

    def test_a_failing_suite_exits_1_naming_the_first_failing_test_and_keeps_the_old_live_build(self):
        seams = Seams(suite_code=1)
        code, out, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("deploy refused: suite failed at tests/test_x.py::Case::test_y: 1766 passed, 1 failed", err)
        self.assertEqual(seams.links["venv-decomp"], OLD)
        self.assertEqual((seams.commands("mv -T"), seams.commands("make -k"), out), ([], [], ""))

    def test_the_suite_runs_after_the_build_and_before_the_repoint(self):
        seams = Seams()
        run_main(seams, ENVIRON)
        order = [
            "build"
            if "decomp tools deploy" in c
            else "suite"
            if "DEPLOY-SUITE" in c
            else "repoint"
            if "mv -T" in c
            else "readback"
            if "make -k" in c
            else "links"
            for c in seams.ssh_calls
        ]
        self.assertEqual(order, ["links", "build", "suite", "repoint", "links", "readback"])

    def test_the_suite_runs_in_a_slot_at_the_landed_sha_with_the_new_venv_python(self):
        seams = Seams()
        run_main(seams, ENVIRON)
        (command,) = seams.commands("DEPLOY-SUITE")
        self.assertTrue(command.startswith("~/finish/slot bash -c "))
        self.assertTrue(command.endswith(f" suite {SHA} {deploy_rig.REMOTES['gitea']} {NEW}/bin/python {NEW}"))

    def test_a_failing_suite_refuses_even_when_both_links_already_read_the_sha(self):
        seams = Seams(links=(NEW, NEW), suite_code=1)
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("suite failed at tests/test_x.py::Case::test_y", err)
        self.assertEqual(seams.commands("make -k"), [])

    def test_an_import_outside_the_checkout_refuses_even_when_every_test_passed(self):
        installed = f"{NEW}/lib/python3.13/site-packages/abu_decomp/__init__.py"
        seams = Seams(suite_out=suite_output(imported=installed))
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn(f"abu_decomp from {installed}, outside its checkout", err)
        self.assertEqual(seams.commands("mv -T"), [])

    def test_a_sibling_directory_sharing_the_checkout_prefix_is_outside(self):
        seams = Seams(suite_out=suite_output(imported=f"{CHECKOUT}-old/src/abu_decomp/__init__.py"))
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("outside its checkout", err)

    def test_a_suite_at_another_sha_or_with_no_facts_refuses(self):
        for out in (suite_output(sha="c" * 40), "fatal: unable to access gitea", ""):
            seams = Seams(suite_out=out)
            code, _, err = run_main(seams, ENVIRON)
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertEqual(seams.commands("mv -T"), [])

    def test_a_suite_exiting_nonzero_without_a_named_failure_still_refuses(self):
        seams = Seams(suite_code=1, suite_out=suite_output())
        code, _, err = run_main(seams, ENVIRON)
        self.assertEqual(code, 1)
        self.assertIn("suite failed at", err)
        self.assertEqual(seams.commands("mv -T"), [])

    def test_the_log_carries_the_summary_the_sha_and_the_imported_path(self):
        code, out, _ = run_main(Seams(), ENVIRON)
        self.assertEqual(code, 0)
        self.assertIn(f"{SHA[:12]}: suite {SUMMARY}", out)
        self.assertIn(f"{SHA[:12]}: suite sha {SHA}", out)
        self.assertIn(f"{SHA[:12]}: suite imported {CHECKOUT}/src/abu_decomp/__init__.py", out)

    def test_the_remote_suite_script_parses(self):
        script = deploy_rig.SUITE.replace("__RUNNER__", deploy_rig.RUNNER)
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True, check=False).returncode, 0)


def git(*args, cwd):
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


class SuiteScriptTests(unittest.TestCase):
    """The remote SUITE script and RUNNER, run for real against a tiny local repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # the deploy layout: the checkout sits under a directory that has a pyproject of its own (the outer repo's .tmp)
        base = Path(self.tmp.name) / "outer"
        self.repo, self.home, self.dest = base / "repo", base / "home", base / "dest"
        for path in (self.repo / "src/abu_decomp", self.repo / "tests", self.home / "finish", self.dest):
            path.mkdir(parents=True)
        (base / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.repo / "pyproject.toml").write_text('[project]\nname = "tiny"\n')  # no pytest section
        (self.repo / "src/abu_decomp/__init__.py").write_text("VALUE = 1\n")
        self.write_test("assert abu_decomp.VALUE == 1")
        git("init", "-q", "-b", "main", cwd=self.repo)
        self.commit()

    def write_test(self, body):
        (self.repo / "tests/test_it.py").write_text(
            f"import abu_decomp\n\n\ndef test_it():\n    {body}\n\n\ndef test_other():\n    pass\n"
        )

    def sha(self):
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self):
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "c", cwd=self.repo)
        return self.sha()

    def run_suite(self, sha, env=None):
        command = deploy_rig.suite_command(sha, sys.executable, str(self.dest), str(self.repo))
        done = subprocess.run(
            ["bash", "-c", command.removeprefix("~/finish/slot ")],
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(self.home),
                "PYTHONUSERBASE": site.getuserbase(),  # the interpreter under test keeps finding pytest
                **(env or {}),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return done, deploy_rig.suite_facts(done.stdout)

    def test_a_passing_suite_reports_the_import_from_the_checkout_and_writes_suite_txt(self):
        sha = self.sha()
        done, facts = self.run_suite(sha)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(facts["suite-sha"], sha)
        self.assertTrue(facts["suite-summary"].startswith("2 passed, 0 failed, 0 skipped in "))
        self.assertTrue(PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]))
        text = (self.dest / "suite.txt").read_text()
        self.assertIn(f"suite-sha: {sha}", text)
        self.assertIn("suite-import: ", text)
        self.assertEqual(list((self.home / "finish").glob("DEPLOY-SUITE.*")), [])  # the checkout is removed

    def test_a_failing_test_exits_1_names_the_first_failure_and_writes_no_suite_txt(self):
        self.write_test("assert abu_decomp.VALUE == 2")
        done, facts = self.run_suite(self.commit())
        self.assertEqual(done.returncode, 1)
        self.assertEqual(facts["suite-first-failure"], "tests/test_it.py::test_it")
        self.assertTrue(facts["suite-summary"].startswith("1 passed, 1 failed, 0 skipped in "))
        self.assertFalse((self.dest / "suite.txt").exists())

    def test_a_test_printing_without_a_newline_does_not_swallow_the_facts(self):
        self.write_test("import sys; sys.stdout.write('kept-by-default')")
        done, facts = self.run_suite(self.commit())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(facts["suite-summary"].startswith("2 passed, 0 failed, 0 skipped in "))

    def test_subtests_are_not_counted_as_tests_and_a_failing_one_is_named(self):
        body = (
            "import unittest\n\n\nclass Case(unittest.TestCase):\n    def test_many(self):\n"
            "        for n in range(3):\n            with self.subTest(n=n):\n"
            "                self.assertNotEqual(n, %s)\n"
        )
        (self.repo / "tests/test_it.py").write_text(body % 9)
        done, facts = self.run_suite(self.commit())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(facts["suite-summary"].startswith("1 passed, 0 failed, 0 skipped in "))
        (self.repo / "tests/test_it.py").write_text(body % 1)
        done, facts = self.run_suite(self.commit())
        self.assertEqual(done.returncode, 1)
        self.assertIn("test_many", facts["suite-first-failure"])

    def test_zero_collected_tests_is_a_failure_not_a_pass(self):
        (self.repo / "tests/test_it.py").unlink()
        done, facts = self.run_suite(self.commit())
        self.assertEqual(done.returncode, 1)
        self.assertIn("suite-first-failure", facts)

    def test_the_suite_runs_exactly_the_landed_sha_not_the_branch_tip(self):
        landed = self.sha()
        self.write_test("assert False, 'a later commit'")
        self.commit()
        done, facts = self.run_suite(landed)
        self.assertEqual((done.returncode, facts["suite-sha"]), (0, landed))

    def test_an_unknown_sha_fails_before_any_test_runs(self):
        done, facts = self.run_suite("f" * 40)
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("suite-import", facts)

    def test_an_installed_copy_ahead_on_the_path_is_never_imported(self):
        installed = Path(self.tmp.name) / "site/abu_decomp"
        installed.mkdir(parents=True)
        (installed / "__init__.py").write_text("VALUE = 99\n")
        done, facts = self.run_suite(self.sha(), env={"PYTHONPATH": str(installed.parent)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]))


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


class SerializedTests(unittest.TestCase):
    """One rig lock per deploy: taken before any seam, held through the readback, freed on every ending."""

    def test_the_lock_wraps_every_git_and_ssh_call_from_the_first_to_the_readback(self):
        seams = Seams()
        deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertEqual(seams.events[0], "lock")
        self.assertEqual(seams.events[-1], "unlock")
        self.assertEqual(seams.events.count("lock") + seams.events.count("unlock"), 2)
        self.assertIn("make -k -j8 check", seams.ssh_calls[-1])

    def test_the_lock_is_freed_when_the_deploy_refuses(self):
        for seams in (Seams(github=False), Seams(suite_code=1), Seams(deploy_code=3)):
            with self.assertRaises(deploy_rig.Refused):
                deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
            self.assertEqual(seams.events[0], "lock")
            self.assertEqual(seams.events[-1], "unlock")

    def test_the_wait_note_leads_the_log(self):
        seams = Seams(notes=["another deploy holds it"])
        self.assertEqual(deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)[0], "another deploy holds it")

    def test_main_takes_the_rig_lock_seam(self):
        seams = Seams()
        code, _, _ = run_main(seams, ENVIRON)
        self.assertEqual((code, seams.events[0], seams.events[-1]), (0, "lock", "unlock"))

    def test_two_deploys_through_one_lock_never_interleave_and_the_second_skips(self):
        mutex, events, seams, logs = threading.Lock(), [], Seams(), {}

        class Shared(FakeLock):
            def __enter__(self):
                mutex.acquire()
                events.append(("lock", threading.current_thread().name))
                return []

            def __exit__(self, *exc):
                events.append(("unlock", threading.current_thread().name))
                mutex.release()
                return False

        def seam(kind, inner):
            def call(*args):
                events.append((kind, threading.current_thread().name))
                return inner(*args)

            return call

        def work():
            logs[threading.current_thread().name] = deploy_rig.deploy(
                SHA, seam("ssh", seams.ssh), seam("git", seams.git), Shared()
            )

        threads = [threading.Thread(target=work, name=name) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        first = events[0][1]
        held = {who for _, who in events[: events.index(("unlock", first))]}
        self.assertEqual(held, {first})
        self.assertEqual(len(seams.commands("decomp tools deploy")), 1)
        self.assertTrue(any("build and swap skipped" in line for lines in logs.values() for line in lines))

    def test_the_hold_script_parses_and_prints_waiting_then_locked(self):
        self.assertEqual(subprocess.run(["bash", "-n"], input=deploy_rig.HOLD, text=True, check=False).returncode, 0)
        self.assertLess(deploy_rig.HOLD.index("waiting"), deploy_rig.HOLD.index("locked"))
        self.assertIn("flock 7", deploy_rig.HOLD)
        self.assertEqual(deploy_rig.LOCK, "deploy-decomp.lock")


class PromotionTests(unittest.TestCase):
    """Under the lock the deploy builds the newest landed main, never an older one."""

    def test_a_descendant_main_head_is_deployed_instead_and_the_log_says_the_older_deploy_deferred(self):
        seams = Seams(head=NEWER)
        lines = deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertEqual(seams.commands("decomp tools deploy"), [deploy_rig.deploy_command(NEWER)])
        self.assertNotIn(SHA, " ".join(seams.ssh_calls))
        moved = f"{SHA[:12]}: main has moved to {NEWER[:12]}, deploying main's head; the older deploy deferred"
        self.assertIn(moved, lines)
        self.assertEqual(set(seams.links.values()), {f"/home/abu/venv-decomp-stage.{NEWER[:12]}"})

    def test_an_older_deploy_after_the_newer_one_ends_on_the_newer_directory_without_a_build(self):
        newer = f"/home/abu/venv-decomp-stage.{NEWER[:12]}"
        seams = Seams(head=NEWER, links=(newer, newer))
        lines = deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertEqual(seams.commands("decomp tools deploy") + seams.commands("mv -T"), [])
        self.assertEqual(set(seams.links.values()), {newer})
        self.assertTrue(any("deferred" in line for line in lines))

    def test_the_head_itself_is_deployed_without_a_promotion_line(self):
        seams = Seams(head=SHA)
        lines = deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertFalse(any("deferred" in line for line in lines))

    def test_a_main_that_is_not_a_descendant_refuses_before_any_rig_command(self):
        seams = Seams(gitea=False, head=NEWER)
        with self.assertRaisesRegex(deploy_rig.Refused, "is not on gitea main"):
            deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertEqual(seams.ssh_calls, [])

    def test_a_head_that_github_main_lacks_refuses_and_names_the_head(self):
        seams = Seams(github=False, head=NEWER)
        with self.assertRaisesRegex(deploy_rig.Refused, f"{NEWER[:12]} is not on github main"):
            deploy_rig.deploy(SHA, seams.ssh, seams.git, seams.lock)
        self.assertEqual(seams.ssh_calls, [])


class SuiteIsolationTests(unittest.TestCase):
    """Two suite runs of the same SHA at once (the real SUITE script and runner) each import only their own checkout."""

    def test_concurrent_runs_of_one_sha_use_their_own_checkout_log_and_slots(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            tmp = Path(tmp)
            (tmp / "home/finish").mkdir(parents=True)
            repo = tmp / "repo"
            files = {
                "src/abu_decomp/__init__.py": "",
                "tests/test_ok.py": "def test_ok():\n    pass\n",
                "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            }
            for name, body in files.items():
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                (repo / name).write_text(body)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
            sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            script = deploy_rig.SUITE.replace("__RUNNER__", deploy_rig.RUNNER)
            procs = []
            for n in (1, 2):
                dest = tmp / f"dest{n}"
                dest.mkdir()
                procs.append(
                    subprocess.Popen(
                        ["bash", "-c", script, "suite", sha, str(repo), sys.executable, str(dest)],
                        env={**os.environ, "HOME": str(tmp / "home"), "PYTHONUSERBASE": site.getuserbase()},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                )
            facts = []
            for proc in procs:
                out = proc.communicate()[0]
                self.assertEqual(proc.returncode, 0, out)
                facts.append(deploy_rig.suite_facts(out))
            for fact in facts:
                self.assertTrue(PurePosixPath(fact["suite-import"]).is_relative_to(fact["suite-checkout"]), fact)
            self.assertNotEqual(facts[0]["suite-checkout"], facts[1]["suite-checkout"])
            self.assertNotEqual(facts[0]["suite-log"], facts[1]["suite-log"])
            for fact in facts:
                self.assertIn(fact["suite-checkout"], Path(fact["suite-log"]).read_text())

    def test_the_suite_script_names_no_path_that_two_runs_of_one_sha_share(self):
        self.assertIn('log=$(mktemp "$HOME/finish/deploy-suite-', deploy_rig.SUITE)
        self.assertIn("ABU_SLOTS_DIR=$(mktemp -d", deploy_rig.SUITE)
        self.assertIn("TMPDIR=$PWD/.tmp", deploy_rig.SUITE)
        self.assertIn("PYTHONPATH=$PWD/src", deploy_rig.SUITE)

    def test_the_readback_worktree_is_unique_per_run(self):
        self.assertIn('wt=$(mktemp -d "$HOME/code/wt/DEPLOY-READBACK.XXXXXX")', deploy_rig.READBACK)


if __name__ == "__main__":
    unittest.main()
