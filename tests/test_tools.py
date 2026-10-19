import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp import tools
from abu_decomp.config import ConfigError, load_profile

UNBAKE = "unbake @ git+https://github.com/AbuN64/cake-unbake-64@" + "a" * 40
RECIPE = "recipemaker @ git+https://github.com/AbuN64/recipe-maker-64@" + "b" * 40
TASTE = "cake-taste-64 @ git+https://github.com/AbuN64/cake-taste-64@" + "c" * 40


class Distribution:
    def __init__(self, url=None, commit=None, requires=(), version="1"):
        self.requires, self.version = list(requires), version
        self.origin = None if url is None else {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}

    def read_text(self, name):
        assert name == "direct_url.json"
        return None if self.origin is None else json.dumps(self.origin)


def installed(**changes):
    table = {
        "abu-decomp-64": Distribution(requires=[UNBAKE, RECIPE, TASTE, "ruff==0.16.9; extra == 'dev'"]),
        "unbake": Distribution("https://github.com/AbuN64/cake-unbake-64", "a" * 40),
        "recipemaker": Distribution(
            "https://github.com/AbuN64/recipe-maker-64.git", "b" * 40, [UNBAKE, "pycparser==3.0"]
        ),
        "cake-taste-64": Distribution("https://github.com/AbuN64/cake-taste-64", "c" * 40, ["textual==8.2.8"]),
    }
    table.update(changes)
    table = {name: value for name, value in table.items() if value is not None}

    def distribution(name):
        if name not in table:
            raise tools.metadata.PackageNotFoundError(name)
        return table[name]

    return mock.patch("abu_decomp.tools.metadata.distribution", side_effect=distribution)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "project").mkdir()
        (root / "host.toml").write_text("")
        (root / "taste.json").write_text("{}")
        tools_section = {"project": "project", "host": "host.toml", "timeout_seconds": 5, "taste_profile": "taste.json"}
        (root / "seat.json").write_text(json.dumps({"schema_version": 1, "name": "seat", "tools": tools_section}))
        self.profile = load_profile(root / "seat.json")
        self.root = root

    def test_taste_without_a_seat_profile_names_the_key_and_what_it_takes(self):
        table = json.loads((self.root / "seat.json").read_text())
        del table["tools"]["taste_profile"]
        (self.root / "seat.json").write_text(json.dumps(table))
        chosen = tools.settings(load_profile(self.root / "seat.json"))
        with self.assertRaises(tools.DependencyError) as caught:
            tools._command("taste", chosen, ("repro", "status"))
        self.assertIn("tools.taste_profile", caught.exception.detail)
        self.assertIn("absolute path", caught.exception.detail)

    def test_pins_come_from_own_metadata_and_match_installed_commits(self):
        with installed():
            report = tools.verify_dependencies()
        self.assertEqual(list(report), ["unbake", "recipe", "taste"])
        self.assertEqual(report["taste"]["commit"], "c" * 40)

    def test_wrong_commit_missing_tool_and_unbake_mismatch_are_refused(self):
        cases = {
            "dependency.pin": {"unbake": Distribution("https://github.com/AbuN64/cake-unbake-64", "d" * 40)},
            "dependency.missing": {"cake-taste-64": None},
            "dependency.provenance": {"recipemaker": Distribution(requires=[UNBAKE])},
            "dependency.compatibility": {
                "recipemaker": Distribution("https://github.com/AbuN64/recipe-maker-64", "b" * 40, ["unbake>=0"])
            },
        }
        for key, change in cases.items():
            with self.subTest(key=key), installed(**change), self.assertRaises(tools.DependencyError) as caught:
                tools.verify_dependencies()
            self.assertEqual(caught.exception.key, key)

    def test_cake_taste_must_be_standalone(self):
        bad = Distribution("https://github.com/AbuN64/cake-taste-64", "c" * 40, [UNBAKE])
        with installed(**{"cake-taste-64": bad}), self.assertRaisesRegex(tools.DependencyError, "standalone"):
            tools.verify_dependencies()

    def test_passthrough_runs_public_main_isolated_with_profile_context(self):
        envelope = {"ok": True, "result": {}, "findings": [], "stages": [], "invocation": "x"}
        done = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b"")
        with installed(), mock.patch("abu_decomp.tools.subprocess.run", return_value=done) as call:
            result = tools.invoke("unbake", self.profile, ["report", "--next"])
            command = call.call_args.args[0]
            self.assertEqual(command[1:3], ["-I", "-c"])
            self.assertIn("from unbake.cli import main", command[3])
            self.assertEqual(command[4:], ["--project", str(self.root / "project"), "report", "--next"])
            self.assertEqual(call.call_args.kwargs["env"]["UNBAKE_HOST"], str(self.root / "host.toml"))
            # the tool's stages stream live to our stderr: only its stdout JSON is captured
            self.assertEqual(call.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertNotIn("stderr", call.call_args.kwargs)
            self.assertNotIn("capture_output", call.call_args.kwargs)
            self.assertTrue(result.ok)
            tools.invoke("recipe", self.profile, ["status"])
            self.assertEqual(call.call_args.args[0][4:], ["--project", str(self.root / "project"), "status", "--json"])
            tools.invoke("taste", self.profile, ["profile"])
            self.assertEqual(call.call_args.args[0][4:], ["--profile", str(self.root / "taste.json"), "profile"])

    def seat_with_missing_defaults(self):
        """A seat whose tools.project and tools.host name things that do not exist, next to real ones."""
        root = self.root
        (root / "real").mkdir()
        (root / "real.toml").write_text("")
        section = {"project": "gone", "host": "gone.toml", "timeout_seconds": 5, "taste_profile": "taste.json"}
        (root / "bad.json").write_text(json.dumps({"schema_version": 1, "name": "seat", "tools": section}))
        return load_profile(root / "bad.json")

    def test_explicit_project_and_host_win_over_defaults_that_do_not_exist(self):
        profile = self.seat_with_missing_defaults()
        chosen = tools.settings(profile, self.root / "real", self.root / "real.toml")
        self.assertEqual((chosen.project, chosen.host), (self.root / "real", self.root / "real.toml"))
        with self.assertRaises(tools.DependencyError) as caught:
            tools.settings(profile, self.root / "real")  # the host default is the value used, and it is missing
        self.assertIn("tools.host", caught.exception.detail)
        with self.assertRaises(tools.DependencyError) as caught:
            tools.settings(profile)
        self.assertIn("tools.project", caught.exception.detail)

    def test_only_the_value_a_caller_uses_is_validated(self):
        profile = self.seat_with_missing_defaults()
        self.assertEqual(tools.project_dir(profile, self.root / "real"), self.root / "real")
        self.assertEqual(tools.host_file(profile, self.root / "real.toml"), self.root / "real.toml")
        with self.assertRaises(tools.DependencyError):
            tools.project_dir(profile)
        with self.assertRaises(tools.DependencyError):
            tools.host_file(profile)

    def test_a_seat_may_leave_project_and_host_out_when_every_call_names_them(self):
        (self.root / "real").mkdir()
        (self.root / "real.toml").write_text("")
        section = {"timeout_seconds": 5}
        (self.root / "bare.json").write_text(json.dumps({"schema_version": 1, "name": "seat", "tools": section}))
        profile = load_profile(self.root / "bare.json")
        self.assertEqual(tools.settings(profile, self.root / "real", self.root / "real.toml").timeout_seconds, 5.0)
        with self.assertRaises(ConfigError) as caught:
            tools.settings(profile, self.root / "real")
        self.assertIn("missing keys: host", caught.exception.detail)

    def test_taste_runs_without_the_project_and_host_defaults_existing(self):
        profile = self.seat_with_missing_defaults()
        envelope = {"ok": True, "result": {}, "findings": [], "stages": [], "invocation": "x"}
        done = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b"")
        with (
            installed(),
            mock.patch.dict("os.environ"),
            mock.patch("abu_decomp.tools.subprocess.run", return_value=done) as call,
        ):
            os.environ.pop("UNBAKE_HOST", None)
            result = tools.invoke("taste", profile, ["profile"])
            self.assertTrue(result.ok)
            self.assertEqual(call.call_args.kwargs["cwd"], self.root)  # the Taste profile's directory
            self.assertNotIn("UNBAKE_HOST", call.call_args.kwargs["env"])
            self.assertEqual(set(result.context), {"profile_sha256"})
            tools.invoke("taste", profile, ["--help"])
            with self.assertRaises(tools.DependencyError):
                tools.invoke("unbake", profile, ["report"])  # an unbake call still needs a real project and host

    def test_version_and_help_pass_through_as_text(self):
        done = subprocess.CompletedProcess([], 0, b"unbake 0.5.11\n", b"")
        with installed(), mock.patch("abu_decomp.tools.subprocess.run", return_value=done) as call:
            for args, tail in (
                (["--version"], ["--version"]),
                (["score", "--help"], ["score", "--help"]),
                (["-h"], ["-h"]),
            ):
                result = tools.invoke("unbake", self.profile, args)
                self.assertEqual(call.call_args.args[0][4:], ["--project", str(self.root / "project"), *tail])
                self.assertEqual((result.ok, result.text), (True, b"unbake 0.5.11\n"))
            tools.invoke("recipe", self.profile, ["status", "--help"])
            self.assertNotIn("--json", call.call_args.args[0])
            failed = subprocess.CompletedProcess([], 2, b"usage: x\n", b"")
            call.return_value = failed
            self.assertFalse(tools.invoke("unbake", self.profile, ["bogus", "--help"]).ok)
            with self.assertRaises(tools.DependencyError):
                tools.invoke("unbake", self.profile, ["--version", "--project", "x"])
            self.assertFalse(tools.is_text_call(["score", "--version", "us"]))

    def test_version_text_lists_decomp_and_pins(self):
        with installed():
            lines = tools.version_text().splitlines()
        self.assertEqual(lines[0], "decomp 1")
        self.assertEqual([line.split()[0] for line in lines[1:]], ["unbake", "recipe", "taste"])

    def test_profile_owned_arguments_and_bad_output_are_refused(self):
        with installed(), mock.patch("abu_decomp.tools.subprocess.run") as call:
            for tool, args in (("unbake", ["report", "--host=x"]), ("taste", ["--profile", "x"]), ("recipe", [])):
                with self.subTest(tool=tool, args=args), self.assertRaises(tools.DependencyError):
                    tools.invoke(tool, self.profile, args)
            call.assert_not_called()
        bad = subprocess.CompletedProcess([], 0, b'{"ok": true}', b"")
        with (
            installed(),
            mock.patch("abu_decomp.tools.subprocess.run", return_value=bad),
            self.assertRaisesRegex(tools.DependencyError, "envelope"),
        ):
            tools.invoke("unbake", self.profile, ["report"])

    def test_cache_totals_are_the_outermost_stage_not_the_sum_over_the_nesting(self):
        stages = {
            "path": ["score"],
            "cache": {"build": [1, 2]},
            "children": [{"path": ["score", "context"], "cache": {"build": [1, 2]}, "children": []}],
        }
        summary = tools.cache_summary(tools.cache_report(stages))
        self.assertEqual((summary["hits"], summary["misses"]), (1, 2))  # the child's lookups are already the parent's
        self.assertEqual(set(summary["stages"]), {"score", "score > context"})


if __name__ == "__main__":
    unittest.main()
