import importlib.util
import io
import json
import os
import site
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath

spec = importlib.util.spec_from_file_location("deploy_rig", Path(__file__).parent.parent / "ci" / "deploy_rig.py")
deploy_rig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_rig)

SHA = "a" * 12 + "b" * 28
NEWER = "d" * 12 + "e" * 28
TOOLKIT = "/opt/example/venv-decomp"
BUILD = f"{TOOLKIT}.{SHA[:12]}"
ENVIRON = {deploy_rig.ENV: SHA}
ARGS = ["--board", "board1", "--card", "card1", "--toolkit", TOOLKIT]
CHECKOUT = "/srv/runs/board1.card1/Projects/AbuN64/AbuDecomp64"
SUMMARY = "1767 passed, 0 failed, 5 skipped in 204 s"


def suite_log(sha=SHA, imported=f"{CHECKOUT}/src/abu_decomp/__init__.py", failed=None, summary=True):
    lines = [f"deploy-build: {TOOLKIT}-stage.{sha[:12]}", f"suite-checkout: {CHECKOUT}", f"suite-sha: {sha}"]
    lines.append(f"suite-import: {imported}")
    if failed:
        lines += ["suite-summary: 1766 passed, 1 failed, 5 skipped in 204 s", f"suite-first-failure: {failed}"]
    elif summary:
        lines.append(f"suite-summary: {SUMMARY}")
    return "\n".join(lines) + "\n"


def answer(exit_code=0, log="/runs/.tmp/job-rig-1.log"):
    return deploy_rig.Answer({"Exit": exit_code, "Log": log, "Width": 16, "Host": "rig"})


class Rig:
    """The seams: Job Invoke answers by call order (activation, readback), git knows the two mains, logs are text."""

    def __init__(self, gitea=True, github=True, head=None, activation=0, readback=0, suite=None, refuse_at=None):
        self.head, self.on = head, {"gitea": gitea, "github": github}
        self.exits = [activation, readback]
        self.logs = {
            "/runs/.tmp/job-rig-1.log": suite if suite is not None else suite_log(head or SHA),
            "/runs/.tmp/job-rig-2.log": f"readback-pin: abu-decomp-64 {head or SHA}\n",
        }
        self.refuse_at = refuse_at
        self.requests, self.git_calls, self.fetched = [], [], None
        self.have = True

    def job(self, body):
        self.requests.append(body)
        n = len(self.requests)
        if self.refuse_at == n:
            raise deploy_rig.Refused("Job Invoke refused Conflict (exit 1): the build or gate exited 1")
        return answer(self.exits[n - 1], f"/runs/.tmp/job-rig-{n}.log")

    def read(self, path):
        return self.logs[path]

    def git(self, *args):
        self.git_calls.append(args)
        if "rev-parse" in args:
            return 0, self.head or ""
        if "fetch" in args and "-C" in args:
            self.fetched = next(name for name, url in deploy_rig.REMOTES.items() if url in args)
            return 0, ""
        if "fetch" in args:
            self.have = True
            return 0, ""
        if "merge-base" in args:
            return (0 if self.on[self.fetched] else 1), ""
        if "cat-file" in args:
            return (0 if self.have else 1), ""
        return 0, ""

    def deploy(self, sha=SHA):
        return deploy_rig.deploy(sha, "board1", "card1", TOOLKIT, self.job, self.git, self.read)


def run_main(rig, argv=ARGS, environ=ENVIRON):
    saved = deploy_rig.job_invoke, deploy_rig.git, deploy_rig.read_log
    deploy_rig.job_invoke, deploy_rig.git, deploy_rig.read_log = rig.job, rig.git, rig.read
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = deploy_rig.main(argv, environ)
    finally:
        deploy_rig.job_invoke, deploy_rig.git, deploy_rig.read_log = saved
    return code, out.getvalue(), err.getvalue()


class JobInvokeAnswerTests(unittest.TestCase):
    def test_a_success_answer_carries_exit_log_and_width(self):
        body = {"Metadata": {"Result": "Success"}, "Response": {"Exit": 0, "Log": "/x.log", "Width": 16, "Host": "rig"}}
        got = deploy_rig.answer_of(0, json.dumps(body))
        self.assertEqual((got["Exit"], got["Log"], got["Width"]), (0, "/x.log", 16))

    def test_a_nonzero_job_exit_is_an_answer_not_a_refusal(self):
        got = deploy_rig.answer_of(0, json.dumps({"Response": {"Exit": 2, "Log": "/x.log"}}))
        self.assertEqual(got["Exit"], 2)

    def test_a_refusal_names_its_code_message_and_next_step(self):
        error = {"Code": "Conflict", "Message": "the build or gate exited 1", "NextStep": "read the log", "Status": 409}
        with self.assertRaises(deploy_rig.Refused) as caught:
            deploy_rig.answer_of(1, json.dumps({"Response": {"Error": error}}))
        self.assertEqual(
            str(caught.exception),
            "Job Invoke refused Conflict (exit 1): the build or gate exited 1; next: read the log",
        )

    def test_unparseable_output_or_a_fault_exit_or_a_missing_log_is_refused(self):
        for code, out in (
            (2, "panic: no socket"),
            (0, ""),
            (0, json.dumps({"Metadata": {}})),
            (2, json.dumps({"Response": {"Exit": 0, "Log": "/x.log"}})),
            (0, json.dumps({"Response": {"Exit": 0}})),
            (0, json.dumps({"Response": {"Exit": "0", "Log": "/x.log"}})),
        ):
            with self.subTest(code=code, out=out), self.assertRaises(deploy_rig.Refused):
                deploy_rig.answer_of(code, out)

    def test_job_invoke_calls_the_public_cli_with_the_request_on_stdin(self):
        calls = []

        def fake(argv, stdin=None):
            calls.append((argv, stdin))
            return 0, json.dumps({"Response": {"Exit": 0, "Log": "/x.log"}})

        saved, deploy_rig.run = deploy_rig.run, fake
        try:
            deploy_rig.job_invoke({"Host": "rig"})
        finally:
            deploy_rig.run = saved
        argv, stdin = calls[0]
        self.assertEqual(argv, ["mo-butlermaster", "Job", "Invoke", "--input=json", "--output=json"])
        self.assertEqual(json.loads(stdin), {"Metadata": {}, "Request": {"Host": "rig"}})


class DeployTests(unittest.TestCase):
    def test_the_activation_job_names_the_worktree_toolkit_and_revision_and_the_readback_job_has_no_activation(self):
        rig = Rig()
        rig.deploy()
        activation, readback = rig.requests
        for body in rig.requests:
            self.assertEqual(body["Host"], "rig")
            self.assertEqual(body["Subject"], {"Board": "board1", "Card": "card1"})
            self.assertEqual(body["Worktree"], f"Projects/AbuN64/AbuDecomp64@{SHA}")
            self.assertIsInstance(body["Argv"], list)
        self.assertEqual(activation["Activation"], {"Toolkit": TOOLKIT, "Revision": SHA[:12]})
        self.assertEqual(activation["Argv"][-2:], [SHA, TOOLKIT])
        self.assertNotIn("Activation", readback)
        self.assertEqual(readback["Argv"][-3:], [SHA, TOOLKIT, BUILD])

    def test_main_prints_the_deployed_sha_with_each_jobs_exit_and_log(self):
        code, out, err = run_main(Rig())
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"deployed {SHA}", out.splitlines())
        self.assertIn(f"{SHA[:12]}: activation job Exit 0, Log /runs/.tmp/job-rig-1.log", out)
        self.assertIn(f"{SHA[:12]}: readback job Exit 0, Log /runs/.tmp/job-rig-2.log", out)
        self.assertIn(f"{SHA[:12]}: suite {SUMMARY}", out)

    def test_the_deploy_never_takes_the_old_ssh_slot_flock_or_waitfor_path(self):
        source = (Path(__file__).parent.parent / "ci" / "deploy_rig.py").read_text()
        code = source.split('"""', 2)[2]
        for word in ("ssh", "flock", "finish/slot", "waitfor", "deploy-decomp.lock", "mv -T"):
            self.assertNotIn(word, code)

    def test_a_sha_not_on_either_main_refuses_before_any_job(self):
        for missing in ("gitea", "github"):
            rig = Rig(**{missing: False})
            code, out, err = run_main(rig)
            self.assertEqual(code, 1)
            self.assertEqual(err.strip(), f"deploy refused: {SHA[:12]} is not on {missing} main")
            self.assertEqual((out, rig.requests), ("", []))

    def test_main_moved_to_a_descendant_deploys_the_head(self):
        rig = Rig(head=NEWER)
        lines = rig.deploy()
        moved = f"{SHA[:12]}: main has moved to {NEWER[:12]}, deploying main's head; the older deploy deferred"
        self.assertIn(moved, lines)
        self.assertEqual(rig.requests[0]["Worktree"], f"Projects/AbuN64/AbuDecomp64@{NEWER}")
        self.assertEqual(rig.requests[0]["Activation"]["Revision"], NEWER[:12])
        self.assertIn(f"deployed {NEWER}", lines)

    def test_a_commit_missing_here_is_fetched_and_still_missing_is_refused(self):
        rig = Rig()
        rig.have = False
        rig.git = lambda *args, _git=rig.git: (1, "") if "cat-file" in args else _git(*args)
        with self.assertRaises(deploy_rig.Refused) as caught:
            rig.deploy()
        self.assertIn("cannot push it to the rig", str(caught.exception))
        self.assertEqual(rig.requests, [])
        fetched = Rig()
        fetched.have = False
        fetched.deploy()
        self.assertTrue(any(call[:2] == ("fetch", "-q") for call in fetched.git_calls))

    def test_a_bad_toolkit_or_sha_refuses_before_any_call(self):
        for argv, environ in (
            (["--board", "b", "--card", "c", "--toolkit", "venv"], ENVIRON),
            (["--board", "b", "--card", "c", "--toolkit", "/a/../venv"], ENVIRON),
            (ARGS, {}),
            (ARGS, {deploy_rig.ENV: "main"}),
        ):
            rig = Rig()
            code, _, err = run_main(rig, argv, environ)
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertEqual((rig.git_calls, rig.requests), ([], []))


class RefusalTests(unittest.TestCase):
    def test_a_failed_gate_refuses_with_the_job_log_line_and_runs_no_readback(self):
        rig = Rig(activation=1, suite=suite_log(failed="tests/test_x.py::Case::test_y"))
        code, _, err = run_main(rig)
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("activation exit 1, links unchanged", err)
        self.assertIn("/runs/.tmp/job-rig-1.log", err)
        self.assertEqual(len(rig.requests), 1)

    def test_a_job_invoke_refusal_stops_the_deploy(self):
        for refuse_at, jobs in ((1, 1), (2, 2)):
            rig = Rig(refuse_at=refuse_at)
            code, _, err = run_main(rig)
            self.assertEqual(code, 1)
            self.assertIn("Job Invoke refused Conflict", err)
            self.assertEqual(len(rig.requests), jobs)

    def test_a_passing_job_whose_log_names_a_failing_test_or_no_summary_is_refused(self):
        failed = suite_log(failed="tests/test_x.py::test_y")
        with self.assertRaisesRegex(deploy_rig.Refused, r"suite failed at tests/test_x.py::test_y: 1766 passed"):
            Rig(suite=failed).deploy()
        with self.assertRaisesRegex(deploy_rig.Refused, "suite gate did not finish"):
            Rig(suite=suite_log(summary=False)).deploy()
        with self.assertRaisesRegex(deploy_rig.Refused, "suite gate did not finish"):
            Rig(suite="killed\n").deploy()

    def test_a_suite_at_another_sha_or_importing_outside_its_worktree_is_refused(self):
        with self.assertRaisesRegex(deploy_rig.Refused, f"suite ran at {NEWER}, not the landed {SHA}"):
            Rig(suite=suite_log(sha=NEWER)).deploy()
        outside = suite_log(imported="/home/abu/venv/lib/abu_decomp/__init__.py")
        with self.assertRaisesRegex(deploy_rig.Refused, "outside its checkout"):
            Rig(suite=outside).deploy()
        sibling = suite_log(imported=f"{CHECKOUT}-other/src/abu_decomp/__init__.py")
        with self.assertRaisesRegex(deploy_rig.Refused, "outside its checkout"):
            Rig(suite=sibling).deploy()

    def test_a_readback_mismatch_refuses_with_the_readback_line(self):
        rig = Rig(readback=1)
        rig.logs["/runs/.tmp/job-rig-2.log"] = "readback: unbake is none, the pin is " + "c" * 40 + "\n"
        code, _, err = run_main(rig)
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(),
            f"deploy refused: readback exit 1: readback: unbake is none, the pin is {'c' * 40} "
            "(/runs/.tmp/job-rig-2.log)",
        )


def wrapper(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def sh_env(**extra):
    return {"PATH": os.environ["PATH"], "PYTHONUSERBASE": site.getuserbase(), **extra}


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bash(self, script, *args, cwd=None, env=None):
        return subprocess.run(
            ["bash", "-c", script, "job", *args],
            cwd=cwd,
            env=env or sh_env(),
            capture_output=True,
            text=True,
            check=False,
        )


def git(*args, cwd):
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True)


class SuiteScriptTests(ScriptCase):
    """The SUITE script and RUNNER, run for real in a tiny worktree with a stand-in BUILD venv."""

    def setUp(self):
        super().setUp()
        self.repo, self.built = self.root / "outer" / "repo", self.root / "built"
        for path in (self.repo / "src/abu_decomp", self.repo / "tests"):
            path.mkdir(parents=True)
        (self.root / "outer/pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        (self.repo / "pyproject.toml").write_text('[project]\nname = "tiny"\n')
        (self.repo / "src/abu_decomp/__init__.py").write_text("VALUE = 1\n")
        self.write_test("assert abu_decomp.VALUE == 1")
        wrapper(self.built / "bin/python", f'exec "{sys.executable}" "$@"')
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "c", cwd=self.repo)

    def write_test(self, body):
        (self.repo / "tests/test_it.py").write_text(
            f"import abu_decomp\n\n\ndef test_it():\n    {body}\n\n\ndef test_other():\n    pass\n"
        )

    def suite(self):
        script = deploy_rig.SUITE.replace("__RUNNER__", deploy_rig.RUNNER)
        done = self.bash(script, cwd=self.repo, env=sh_env(BUILD=str(self.built), built=str(self.built)))
        return done, deploy_rig.suite_facts(done.stdout)

    def test_a_passing_suite_reports_the_import_from_the_worktree_and_writes_suite_txt(self):
        done, facts = self.suite()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(facts["suite-sha"], sha)
        self.assertEqual(facts["suite-checkout"], str(self.repo.resolve()))
        self.assertTrue(facts["suite-summary"].startswith("2 passed, 0 failed, 0 skipped in "))
        self.assertTrue(PurePosixPath(facts["suite-import"]).is_relative_to(facts["suite-checkout"]))
        self.assertEqual(deploy_rig.suite(sha, "log", done.stdout)[0], f"{sha[:12]}: suite {facts['suite-summary']}")
        self.assertIn(f"suite-sha: {sha}", (self.built / "suite.txt").read_text())

    def test_a_failing_test_exits_1_names_the_first_failure_and_writes_no_suite_txt(self):
        self.write_test("assert abu_decomp.VALUE == 2")
        done, facts = self.suite()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(facts["suite-first-failure"], "tests/test_it.py::test_it")
        self.assertFalse((self.built / "suite.txt").exists())
        with self.assertRaisesRegex(deploy_rig.Refused, "suite failed at tests/test_it.py::test_it"):
            deploy_rig.suite(facts["suite-sha"], "log", done.stdout)


class BuildScriptTests(ScriptCase):
    """The BUILD script with a stand-in `decomp` that builds `<stage>.<sha12>` and swaps the stage link."""

    def setUp(self):
        super().setUp()
        self.toolkit = self.root / "venv-decomp"
        self.build = self.root / f"venv-decomp.{SHA[:12]}"
        self.stage = self.root / "venv-decomp-stage"

    def decomp(self, body):
        wrapper(self.toolkit / "bin/decomp", body)

    def run_build(self):
        return self.bash(
            deploy_rig.BUILD, SHA, str(self.toolkit), env=sh_env(BUILD=str(self.build), HOME=str(self.root))
        )

    def test_the_build_links_build_to_the_stage_venv_and_never_touches_the_live_link(self):
        live = self.root / "live-target"
        wrapper(
            live / "bin/decomp",
            'test "$1 $2 $3 $4 $5" = "tools deploy --sha ' + SHA + ' --venv" || exit 7\n'
            f'dir="$6.{SHA[:12]}"; mkdir -p "$dir/bin" && ln -sfn "$dir" "$6"',
        )
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        built = self.root / f"venv-decomp-stage.{SHA[:12]}"
        self.assertEqual(self.build.resolve(), built.resolve())
        self.assertEqual(self.stage.resolve(), built.resolve())
        self.assertEqual(self.toolkit.resolve(), live.resolve())
        self.assertIn(f"deploy-build: {built}", done.stdout)

    def test_a_failing_engine_exits_1_and_links_nothing(self):
        live = self.root / "live-target"
        wrapper(live / "bin/decomp", "echo engine said no >&2; exit 3")
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 1)
        self.assertIn("deploy-error: decomp tools deploy exit 3", done.stderr)
        self.assertFalse(self.build.is_symlink())

    def test_a_stage_link_that_reads_another_build_exits_1(self):
        live = self.root / "live-target"
        wrapper(live / "bin/decomp", f'mkdir -p "{self.root}/other" && ln -sfn "{self.root}/other" "$6"')
        self.toolkit.symlink_to(live)
        done = self.run_build()
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a build of", done.stderr)


class ReadbackScriptTests(ScriptCase):
    """The READBACK script against a stand-in venv: a link check, then the installed commit of each pin."""

    PIN = "c" * 40

    def setUp(self):
        super().setUp()
        self.repo = self.root / "worktree"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "tiny-decomp"\n'
            f'dependencies = ["textual==8.2.8", "tool-one @ git+https://example.lan/tool-one@{self.PIN}"]\n'
        )
        self.site = self.root / "site"
        self.venv = self.root / "venv.build"
        wrapper(self.venv / "bin/python", f'PYTHONPATH="{self.site}" exec "{sys.executable}" "$@"')
        self.toolkit = self.root / "venv-live"
        self.toolkit.symlink_to(self.venv)
        self.dist("tiny-decomp", SHA)
        self.dist("tool-one", self.PIN)

    def dist(self, name, commit):
        info = self.site / f"{name.replace('-', '_')}-1.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        direct = {"url": "https://example.lan/x", "vcs_info": {"vcs": "git", "commit_id": commit}}
        (info / "direct_url.json").write_text(json.dumps(direct))

    def readback(self, build=None):
        return self.bash(deploy_rig.READBACK, SHA, str(self.toolkit), str(build or self.venv), cwd=self.repo)

    def test_matching_pins_and_link_exit_0_and_print_each_installed_commit(self):
        done = self.readback()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"readback-pin: tiny-decomp {SHA}", done.stdout)
        self.assertIn(f"readback-pin: tool-one {self.PIN}", done.stdout)

    def test_a_pin_installed_at_another_commit_or_missing_exits_1_naming_it(self):
        self.dist("tool-one", "9" * 40)
        done = self.readback()
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"tool-one is {'9' * 40}, the pin is {self.PIN}", done.stderr)
        for path in (self.site / "tiny_decomp-1.0.dist-info").iterdir():
            path.unlink()
        (self.site / "tiny_decomp-1.0.dist-info").rmdir()
        done = self.readback()
        self.assertIn("tiny-decomp is not installed", done.stderr)

    def test_a_live_link_that_does_not_resolve_to_the_build_exits_1(self):
        other = self.root / "other"
        other.mkdir()
        done = self.readback(build=other)
        self.assertEqual(done.returncode, 1)
        self.assertIn("reads", done.stderr)
        self.assertNotIn("readback-pin", done.stdout)


if __name__ == "__main__":
    unittest.main()
