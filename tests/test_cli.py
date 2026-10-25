import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp.cli import COMMANDS, describe, main
from abu_decomp.config import ConfigError, load_profile


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(argv)
    return code, output.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def profile(self, value):
        path = self.root / "seat.json"
        path.write_text(json.dumps(value))
        return path

    def test_profile_defaults_to_the_seat_config_without_the_flag(self):
        seat = self.profile({"schema_version": 1, "name": "seat", "tools": {}})
        link = self.root / "xdg" / "abu-decomp" / "profile.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(seat)
        other = self.root / "other.json"
        other.write_text(json.dumps({"schema_version": 1, "name": "other", "tools": {}}))
        clean = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "xdg")}
        with mock.patch.dict("os.environ", clean):
            os.environ.pop("DECOMP_PROFILE", None)
            os.environ.pop("DECOMP_TAS_PROFILE", None)
            self.assertEqual(json.loads(run(["profile"])[1])["result"]["name"], "seat")
            os.environ["DECOMP_PROFILE"] = str(other)  # the environment beats the config file
            self.assertEqual(json.loads(run(["profile"])[1])["result"]["name"], "other")
            self.assertEqual(json.loads(run(["--profile", str(seat), "profile"])[1])["result"]["name"], "seat")

    def test_a_sigterm_kill_names_itself_not_a_bad_argument(self):
        with mock.patch("abu_decomp.cli._tool_call", side_effect=SystemExit(143)):
            code, text = run(["data"])
        finding = json.loads(text)["findings"][0]
        self.assertEqual((code, finding["key"]), (143, "command.terminated"))
        self.assertIn("SIGTERM", finding["message"])
        with mock.patch("abu_decomp.cli._tool_call", side_effect=SystemExit(2)):
            self.assertEqual(json.loads(run(["data"])[1])["findings"][0]["key"], "cli.arguments")

    def test_help_is_plain_text_and_lists_every_command(self):
        code, text = run(["--help"])
        self.assertEqual(code, 0)
        for name in COMMANDS:
            self.assertIn(name, text)

    def test_every_command_prints_one_envelope(self):
        path = self.profile({"schema_version": 1, "name": "seat", "tools": {}})
        code, text = run(["--profile", str(path), "profile"])
        self.assertEqual(code, 0)
        envelope = json.loads(text)
        self.assertEqual(set(envelope), {"ok", "result", "findings", "stages", "invocation"})
        self.assertEqual(envelope["result"]["sections"], ["tools"])
        with mock.patch.dict("os.environ", {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "none")}):
            for name in ("DECOMP_PROFILE", "DECOMP_TAS_PROFILE"):
                os.environ.pop(name, None)
            code, text = run(["profile"])
        self.assertEqual((code, json.loads(text)["findings"][0]["key"]), (1, "config.missing"))
        code, text = run(["nonsense"])
        self.assertEqual((code, json.loads(text)["findings"][0]["key"]), (1, "cli.arguments"))

    def test_play_interruption_keeps_signal_exit_code_and_is_not_an_argument_error(self):
        from abu_decomp.tas import PlayInterrupted

        for signum in (2, 15):
            command = SimpleNamespace(
                **{**vars(COMMANDS["profile"]), "run": mock.Mock(side_effect=PlayInterrupted(signum))}
            )
            with mock.patch.dict(COMMANDS, {"profile": command}):
                code, text = run(["profile"])
            envelope = json.loads(text)
            self.assertEqual(code, 128 + signum)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["findings"][0]["key"], "command.interrupted")

    def test_version_needs_no_profile(self):
        with mock.patch("abu_decomp.tools.version_text", return_value="decomp 9\nunbake 1\n"):
            self.assertEqual(run(["--version"]), (0, "decomp 9\nunbake 1\n"))

    def test_tool_arguments_pass_through_untouched_and_text_is_not_wrapped(self):
        path = self.profile({"schema_version": 1, "name": "seat", "tools": {}})
        seen = []

        def invoke(tool, profile, args, **kw):
            seen.append((tool, list(args), kw["project"], kw["host"]))
            return SimpleNamespace(text=b"usage: score\n", returncode=0)

        raw = io.BytesIO()
        out = io.TextIOWrapper(raw)
        with mock.patch("abu_decomp.tools.invoke", invoke), contextlib.redirect_stdout(out):
            for argv in (
                ["--profile", str(path), "unbake", "--version"],
                ["--profile", str(path), "unbake", "--project", "/p", "score", "--help"],
            ):
                self.assertEqual(main(argv), 0)
        self.assertEqual(
            seen, [("unbake", ["--version"], None, None), ("unbake", ["score", "--help"], Path("/p"), None)]
        )
        self.assertEqual(raw.getvalue(), b"usage: score\nusage: score\n")

    def test_profile_rejects_unknown_and_duplicate_keys(self):
        with self.assertRaisesRegex(ConfigError, "unknown keys: project"):
            load_profile(self.profile({"schema_version": 1, "name": "seat", "project": "x"}))
        taste = {"schema_version": 1, "name": "t", "project_root": "x", "handoff": {}}  # another tool's profile
        with self.assertRaisesRegex(ConfigError, "unknown keys: handoff, project_root; this is not a decomp seat"):
            load_profile(self.profile(taste))
        path = self.root / "dup.json"
        path.write_text('{"schema_version": 1, "name": "a", "name": "b"}')
        with self.assertRaisesRegex(ConfigError, "duplicate JSON key"):
            load_profile(path)

    def test_describe_derives_arguments_from_the_parser(self):
        tree = {command["name"]: command for command in describe()["commands"]}
        self.assertEqual(list(tree), list(COMMANDS))
        self.assertEqual(next(row for row in tree["unbake"]["arguments"] if row["name"] == "args")["nargs"], "...")
        port = tree["port"]["arguments"][0]["subcommands"]["new"]
        self.assertEqual([row["name"] for row in port], ["spec", "destination"])


class ModuleEntryTests(unittest.TestCase):
    def test_python_m_cli_prints_the_help(self):
        import os
        import subprocess
        import sys

        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        done = subprocess.run(
            [sys.executable, "-m", "abu_decomp.cli", "--help"], capture_output=True, text=True, env=env
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("phase", done.stdout)


if __name__ == "__main__":
    unittest.main()
