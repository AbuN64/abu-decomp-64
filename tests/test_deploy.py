import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            **extra,
        )

    def test_agreeing_pins_install_git_commits_into_every_venv_and_smoke_test(self):
        result, code, findings = self.deploy(venvs=("venv-a", "venv-b"), unbake=U1)
        self.assertEqual((code, findings), (0, []))
        installs = [c for c in self.commands if "install" in c]
        self.assertEqual(len(installs), 2)
        specs = installs[0][installs[0].index("install") + 1 :]
        self.assertEqual(specs[0], f"abu-decomp-64[dev] @ git+{deploy.DECOMP_URL}@{D1}")
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
        self.assertFalse([f for f, _ in moves if f.startswith(str(self.project))])  # a project is never a pin
        self.assertNotIn(("recipe-maker-64 pyproject.toml", "unbake"), moves)  # R2 already pins U2
        self.assertTrue(all(f["key"] == "deploy.pin_must_move" and f["blocking"] for f in findings))

    def test_the_pinned_packages_of_the_project_are_installed_beside_the_tools(self):
        """RageWars pins splat64 and spimdisasm in requirements.txt: a plain deploy ships them, no hand reinstall."""
        (self.project / "requirements.txt").write_text(
            "# make extract\nsplat64[mips]==0.50.0\nspimdisasm==1.42.4  # x\n"
        )
        result, code, findings = self.deploy(unbake=U1)
        self.assertEqual((code, findings), (0, []))
        self.assertEqual(result["packages"], ["splat64[mips]==0.50.0", "spimdisasm==1.42.4"])
        install = next(c for c in self.commands if "install" in c)
        self.assertEqual(install[-2:], ["splat64[mips]==0.50.0", "spimdisasm==1.42.4"])

    def test_a_project_package_that_is_not_an_exact_pin_refuses_before_anything_is_installed(self):
        (self.project / "requirements.txt").write_text("splat64>=0.50\n")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(unbake=U1)
        self.assertEqual((caught.exception.key, self.commands), ("deploy.requirements", []))

    def test_a_project_file_naming_another_tool_version_never_blocks_a_deploy(self):
        """Game projects build standalone and pin none of our tools: a tool release never asks a project to move."""
        self.pin_file(unbake="0.4.0")
        result, code, findings = self.deploy(unbake=U1)
        self.assertEqual((code, findings, result["pins_to_move"]), (0, [], []))

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

    def test_a_deploy_resyncs_every_work_tree_with_the_new_venv_and_skips_a_held_one(self):
        trees = [self.root / name for name in ("lane-a", "lane-held", "lane-bad", "no-makefile")]
        for tree in trees[:3]:
            tree.mkdir()
            (tree / "Makefile").write_text("")
        trees[3].mkdir()
        calls = []

        def runner(argv, cwd=None):
            if argv[:3] == ["git", "worktree", "list"]:
                return 0, "".join(f"worktree {t}\nHEAD {'0' * 40}\n\n" for t in trees)
            if "sync" in argv:
                calls.append((argv, cwd))
                return (1, "Refused: a literals_ fact names no function") if cwd == trees[2] else (0, "")
            return self.runner(argv, cwd)

        held = lambda tree: [4242] if tree == trees[1] else []  # noqa: E731
        with mock.patch.object(deploy, "holders", held):
            result, code, findings = deploy.deploy(
                self.profile,
                D1,
                [self.root / "venv-a"],
                {"unbake": None, "recipe": None, "taste": None},
                fetch=self.fetch,
                run=runner,
            )
        self.assertEqual((code, findings), (0, []))  # a refused or held tree never fails the deploy
        live = self.root / "venv-a"  # the link, already swapped: the files name the live path, never a versioned one
        self.assertEqual(
            [(Path(argv[0]).parent.parent, argv[-2:], cwd) for argv, cwd in calls],
            [(live, ["sync", "--makefile"], trees[0]), (live, ["sync", "--makefile"], trees[2])],
        )
        self.assertEqual(
            [(Path(r["tree"]).name, r["outcome"]) for r in result["venvs"][0]["resync"]],
            [("lane-a", "synced"), ("lane-held", "skipped"), ("lane-bad", "refused")],
        )
        self.assertEqual(result["venvs"][0]["resync"][1]["held_by"], [4242])
        self.assertIn("literals_", result["venvs"][0]["resync"][2]["tail"])

    def test_holders_names_the_processes_working_inside_a_tree(self):
        tree = self.root / "busy"
        (tree / "deep").mkdir(parents=True)
        child = subprocess.Popen(["sleep", "30"], cwd=tree / "deep")
        self.addCleanup(child.kill)
        self.assertEqual(deploy.holders(tree), [child.pid])
        self.assertEqual(deploy.holders(self.root / "nowhere"), [])

    def test_a_real_directory_is_refused_not_migrated(self):
        self.make_venv(self.root / "venv-c")
        with self.assertRaises(deploy.DeployError) as caught:
            self.deploy(venvs=("venv-c",))
        self.assertEqual(caught.exception.key, "deploy.venv")

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
