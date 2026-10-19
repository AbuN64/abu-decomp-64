import json
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
        for name in ("venv-a", "venv-b"):
            (self.root / name / "bin").mkdir(parents=True)
            (self.root / name / "bin" / "python").write_text("")
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
        if self.fail and self.fail in " ".join(argv):
            return 1, "boom"
        return 0, "ok output"

    def deploy(self, sha=D1, venvs=("venv-a",), **commits):
        return deploy.deploy(
            self.profile,
            sha,
            [self.root / v for v in venvs],
            {"unbake": None, "recipe": None, "taste": None, **commits},
            fetch=self.fetch,
            run=self.runner,
        )

    def test_agreeing_pins_install_git_commits_into_every_venv_and_smoke_test(self):
        result, code, findings = self.deploy(venvs=("venv-a", "venv-b"), unbake=U1)
        self.assertEqual((code, findings), (0, []))
        installs = [c for c in self.commands if "install" in c and "uninstall" not in c]
        self.assertEqual(len(installs), 2)
        specs = installs[0][installs[0].index("install") + 1 :]
        self.assertEqual(specs[0], f"abu-decomp-64 @ git+{deploy.DECOMP_URL}@{D1}")
        self.assertIn(f"unbake @ git+{URL['unbake']}@{U1}", specs)
        self.assertTrue(all("@ git+https://" in spec and ".whl" not in spec for spec in specs))
        self.assertEqual(sum(c[0].endswith("decomp") and c[1] == "dependencies" for c in self.commands), 2)
        symbols = [c for c in self.commands if c[0].endswith("unbake")]
        self.assertEqual(len(symbols), 2)
        self.assertEqual(symbols[0][-3:], ["symbols", "--version", "us-rev1"])
        self.assertIn("--host", symbols[0])
        self.assertEqual(
            [row["venv"] for row in result["venvs"]], [str(self.root / "venv-a"), str(self.root / "venv-b")]
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
        self.fail = "dependencies"
        _, code, findings = self.deploy()
        self.assertEqual((code, findings[0]["key"]), (1, "deploy.smoke"))

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
