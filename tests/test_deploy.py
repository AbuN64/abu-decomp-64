import json
import os
import tempfile
import unittest
from pathlib import Path

from abu_decomp import deploy, tools
from abu_decomp.config import load_profile

U1, U2 = "1" * 40, "2" * 40
R1, R2 = "3" * 40, "4" * 40
T1 = "5" * 40
D1 = "a" * 40
URL = {
    "unbake": "https://github.com/AbuN64/cake-unbake-64",
    "recipemaker": "https://github.com/AbuN64/recipe-maker-64",
    "cake-taste-64": "https://github.com/AbuN64/cake-taste-64",
}


def pyproject(name, version, **pins):
    rows = [f'"{n} @ git+{URL[n]}@{c}"' for n, c in pins.items()]
    return f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{", ".join(rows)}]\n'


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "ci").mkdir(parents=True)
        (self.project / "versions" / "us-rev1").mkdir(parents=True)
        self.pin_file(unbake="0.5.12")
        for name in ("venv-a", "venv-b"):  # live paths: symlinks to a versioned venv
            self.make_venv(self.root / f"{name}.old")
            (self.root / name).symlink_to(self.root / f"{name}.old")
        (self.root / "host.toml").write_text("")
        sections = {"tools": {"project": str(self.project), "host": "host.toml", "timeout_seconds": 5}}
        (self.root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", **sections}))
        self.profile = load_profile(self.root / "seat.json")
        self.documents = {
            (deploy.DECOMP_URL, D1): pyproject(
                "abu-decomp-64", "0.1.0", **{"unbake": U1, "recipemaker": R1, "cake-taste-64": T1}
            ),
            (URL["unbake"], U1): '[project]\nname = "unbake"\ndynamic = ["version"]\n'
            '[tool.setuptools.dynamic]\nversion = { attr = "unbake.__version__" }\n',
            (URL["unbake"], U1, "src/unbake/__init__.py"): '"""x"""\n__version__ = "0.5.12"\n',
            (URL["unbake"], U2): pyproject("unbake", "0.5.13"),
            (URL["recipemaker"], R1): pyproject("recipemaker", "0.1.1", unbake=U1),
            (URL["recipemaker"], R2): pyproject("recipemaker", "0.1.2", unbake=U2),
            (URL["cake-taste-64"], T1): pyproject("cake-taste-64", "0.1.0"),
        }
        self.commands = []
        self.fail = None
        self.busy = {}

    @staticmethod
    def make_venv(path):
        (path / "bin").mkdir(parents=True)
        (path / "bin" / "python").write_text("")

    def pin_file(self, **versions):
        rows = [
            f"pip install https://github.com/x/{name}/releases/download/v{v}/{name}-{v}-py3-none-any.whl"
            for name, v in versions.items()
        ]
        (self.project / "ci" / "run").write_text("\n".join(rows) + "\n")

    def fetch(self, url, sha, path):
        if path != "pyproject.toml":
            if (url, sha, path) not in self.documents:
                raise deploy.DeployError("deploy.fetch", path)
            return self.documents[(url, sha, path)]
        return self.documents[(url, sha)]

    def runner(self, argv, cwd=None):
        self.commands.append(list(argv))
        if "venv" in argv and "-m" in argv and not (self.fail and self.fail in " ".join(argv)):
            self.make_venv(Path(argv[-1]))
        if self.fail and self.fail in " ".join(argv):
            return 1, "boom"
        return 0, "ok output"

    def deploy(self, sha=D1, venvs=("venv-a",), extra=None, **commits):
        extra = extra or {}
        return deploy.deploy(
            self.profile,
            sha,
            [self.root / v for v in venvs],
            {"unbake": None, "recipe": None, "taste": None, **commits},
            fetch=self.fetch,
            run=self.runner,
            users=lambda path: self.busy.get(path.name, []),
            stamp="T",
            **extra,
        )

    def test_agreeing_pins_install_git_commits_into_every_venv_and_smoke_test(self):
        result, code, findings = self.deploy(venvs=("venv-a", "venv-b"), unbake=U1)
        self.assertEqual((code, findings), (0, []))
        installs = [c for c in self.commands if "install" in c]
        self.assertEqual(len(installs), 2)
        specs = installs[0][installs[0].index("install") + 1 :]
        self.assertEqual(specs[0], f"abu-decomp-64 @ git+{deploy.DECOMP_URL}@{D1}")
        self.assertIn(f"unbake @ git+{URL['unbake']}@{U1}", specs)
        self.assertTrue(all("@ git+https://" in spec and ".whl" not in spec for spec in specs))
        self.assertEqual(
            sum(c[0].endswith("decomp") and c[1] == "dependencies" for c in self.commands), 4
        )  # per venv: fresh, then via the link
        symbols = [c for c in self.commands if c[0].endswith("unbake")]
        self.assertEqual(len(symbols), 4)
        self.assertEqual(symbols[0][-3:], ["symbols", "--version", "us-rev1"])
        self.assertIn("--host", symbols[0])
        self.assertEqual(
            [row["venv"] for row in result["venvs"]], [str(self.root / "venv-a"), str(self.root / "venv-b")]
        )
        for name in ("venv-a", "venv-b"):  # swapped to a fresh versioned venv; the old tree is untouched
            self.assertEqual(os.path.realpath(self.root / name), str(self.root / f"{name}.{D1[:12]}"))
            self.assertTrue((self.root / f"{name}.old" / "bin" / "python").is_file())
        self.assertTrue(all(row["swap"]["readback"] for row in result["venvs"]))
        pip = [c for c in installs if c[0].endswith(f"venv-a.{D1[:12]}/bin/python")]
        self.assertEqual(len(pip), 1)  # pip runs by the new venv's own python, never the live link
        self.assertFalse(
            any(c[:1] == [str(self.root / "venv-a" / "bin" / "python")] and "pip" in c for c in self.commands)
        )

    def test_pins_that_disagree_are_named_and_nothing_is_installed(self):
        result, code, findings = self.deploy(unbake=U2, recipe=R2)
        self.assertEqual(code, 1)
        self.assertEqual(self.commands, [])
        moves = {(m["file"], m["pin"]): (m["now"], m["must"]) for m in result["pins_to_move"]}
        self.assertEqual(moves[("abu-decomp-64 pyproject.toml", "unbake")], (U1, U2))
        self.assertEqual(moves[("abu-decomp-64 pyproject.toml", "recipemaker")], (R1, R2))
        self.assertEqual(moves[(str(self.project / "ci" / "run"), "unbake")], ("0.5.12", "0.5.13"))
        self.assertNotIn(("recipe-maker-64 pyproject.toml", "unbake"), moves)  # R2 already pins U2
        self.assertTrue(all(f["key"] == "deploy.pin_must_move" and f["blocking"] for f in findings))

    def test_recipe_pinning_another_unbake_must_move(self):
        result, code, _ = self.deploy(unbake=U2)
        moves = {(m["file"], m["pin"]): (m["now"], m["must"]) for m in result["pins_to_move"]}
        self.assertEqual(moves[("recipe-maker-64 pyproject.toml", "unbake")], (U1, U2))
        self.assertEqual(code, 1)

    def test_only_full_commit_shas_and_real_venvs_are_accepted(self):
        for bad in ("main", "abc1234", "https://x/y.whl"):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError) as caught:
                self.deploy(sha=bad)
            self.assertEqual(caught.exception.key, "deploy.sha")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(unbake="v0.5.12")
        self.assertEqual(caught.exception.key, "deploy.sha")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(venvs=("missing",))
        self.assertEqual(caught.exception.key, "deploy.venv")

    def test_a_failing_install_and_a_failing_smoke_are_findings(self):
        self.fail = "pip install"
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.install"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / "venv-a.old"))  # live untouched
        self.assertFalse((self.root / f"venv-a.{D1[:12]}").exists())
        self.fail = "dependencies"
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.smoke"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / "venv-a.old"))
        self.assertFalse((self.root / f"venv-a.{D1[:12]}").exists())

    def test_a_redeploy_of_one_commit_gets_its_own_versioned_dir(self):
        self.deploy()
        result, code, _ = self.deploy()
        self.assertEqual(code, 0)
        self.assertEqual(result["venvs"][0]["versioned"], str(self.root / f"venv-a.{D1[:12]}-2"))
        self.assertEqual(os.path.realpath(self.root / "venv-a"), str(self.root / f"venv-a.{D1[:12]}-2"))
        self.assertEqual(result["venvs"][0]["swap"]["previous"], str(self.root / f"venv-a.{D1[:12]}"))
        self.assertTrue((self.root / f"venv-a.{D1[:12]}" / "bin" / "python").is_file())

    def test_a_real_directory_needs_migrate_and_an_idle_tree(self):
        real = self.root / "venv-c"
        self.make_venv(real)
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(venvs=("venv-c",))
        self.assertEqual(caught.exception.key, "deploy.migrate")
        self.busy = {"venv-c": [4242]}
        _, code, findings = self.deploy(venvs=("venv-c",), extra={"migrate": True})
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.migrate_busy"))
        self.assertIn("4242", findings[0]["message"])
        self.assertFalse(real.is_symlink())  # nothing moved while a process used it
        self.assertFalse((self.root / f"venv-c.{D1[:12]}").exists())
        self.busy = {}
        result, code, findings = self.deploy(venvs=("venv-c",), extra={"migrate": True})
        self.assertEqual((code, findings), (0, []))
        swap = result["venvs"][0]["swap"]
        self.assertTrue(real.is_symlink() and swap["readback"])
        self.assertEqual(os.path.realpath(real), str(self.root / f"venv-c.{D1[:12]}"))
        self.assertTrue((self.root / "venv-c.legacy-T" / "bin" / "python").is_file())
        self.assertEqual(swap["previous"], str(self.root / "venv-c.legacy-T"))

    def test_users_of_finds_processes_by_argv_and_cwd(self):
        proc = self.root / "proc"
        for pid, argv, cwd in (
            ("11", f"{self.root}/v/bin/python\0x", "/"),
            ("12", "sh\0-c", f"{self.root}/v/lib"),
            ("13", "sh", "/"),
            ("14", f"{self.root}/vv/bin/python", "/"),
        ):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_text(argv)
            (proc / pid / "cwd").symlink_to(cwd)
            (proc / pid / "maps").write_text("")
        (proc / "self").mkdir()
        self.assertEqual(deploy.users_of(self.root / "v", proc), [11, 12])

    def fake_proc(self, rows):
        """rows: pid -> (ppid, argv, environ words); every process has cwd / and no maps."""
        proc = self.root / "proc"
        for pid, (ppid, argv, environ) in rows.items():
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "stat").write_text(f"{pid} (name with ) paren) S {ppid} 1 1 0\n")
            (proc / str(pid) / "cmdline").write_text(argv)
            (proc / str(pid) / "environ").write_text("\0".join(environ))
            (proc / str(pid) / "cwd").symlink_to("/")
            (proc / str(pid) / "maps").write_text("")
        return proc

    def test_users_of_skips_the_deploys_own_ancestors_and_marked_children(self):
        me = os.getpid()
        venv = f"{self.root}/v"
        proc = self.fake_proc(
            {
                me: (500, f"{venv}/bin/decomp\0tools\0deploy\0--venv\0{venv}", []),
                500: (400, f"timeout\0300\0{venv}/bin/decomp\0--venv\0{venv}", []),  # wrapper that started us
                400: (1, f"sh\0-c\0{venv}/bin/decomp", []),
                21: (me, f"{venv}/bin/python\0-m\0pip", [f"{deploy.MARK}={me}"]),  # a child
                22: (1, f"{venv}/bin/python\0collector", ["HOME=/x", f"{deploy.MARK}={me}"]),  # detached grandchild
                23: (1, f"{venv}/bin/python\0job", ["HOME=/x"]),  # an unmarked user
                24: (1, f"{venv}/bin/python\0job", [f"{deploy.MARK}={me + 1}"]),  # another deploy's child
            }
        )
        self.assertEqual(deploy.users_of(self.root / "v", proc), [23, 24])

    def test_run_command_marks_what_it_starts(self):
        code, output = deploy.run_command(["sh", "-c", f"echo ${deploy.MARK}"])
        self.assertEqual((code, output.strip()), (0, str(os.getpid())))

    def test_cache_report_reads_unbake_stage_telemetry(self):
        stages = {
            "path": ["score"],
            "cache": {},
            "children": [
                {"path": ["score", "context"], "cache": {"build": [0, 3], "view": [2, 0]}, "children": []},
                {"path": ["score", "cold"], "cache": {"build": [0, 0]}, "children": []},
            ],
        }
        report = tools.cache_report(stages)
        self.assertEqual(report, {"score > context": {"build": [0, 3], "view": [2, 0]}})
        summary = tools.cache_summary(report)
        self.assertEqual((summary["hits"], summary["misses"]), (2, 3))
        self.assertEqual(summary["stages"]["score > context"]["build"], {"hits": 0, "misses": 3})
        tools.CACHE_LOG[:] = [
            {"call": "unbake score", "report": report},
            {"call": "api build_context", "report": None},
        ]
        drained = tools.drain_cache()
        self.assertEqual((drained["hits"], drained["misses"], drained["unexposed"]), (2, 3, ["api build_context"]))
        self.assertEqual(tools.drain_cache()["calls"], [])


if __name__ == "__main__":
    unittest.main()
