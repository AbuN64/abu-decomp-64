import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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
        code, text = run(["profile"])
        self.assertEqual((code, json.loads(text)["findings"][0]["key"]), (1, "config.missing"))
        code, text = run(["nonsense"])
        self.assertEqual((code, json.loads(text)["findings"][0]["key"]), (1, "cli.arguments"))

    def test_profile_rejects_unknown_and_duplicate_keys(self):
        with self.assertRaisesRegex(ConfigError, "unknown keys: project"):
            load_profile(self.profile({"schema_version": 1, "name": "seat", "project": "x"}))
        path = self.root / "dup.json"
        path.write_text('{"schema_version": 1, "name": "a", "name": "b"}')
        with self.assertRaisesRegex(ConfigError, "duplicate JSON key"):
            load_profile(path)

    def test_describe_derives_arguments_from_the_parser(self):
        tree = {command["name"]: command for command in describe()["commands"]}
        self.assertEqual(list(tree), list(COMMANDS))
        self.assertEqual(tree["unbake"]["arguments"][0]["nargs"], "...")
        port = tree["port"]["arguments"][0]["subcommands"]["new"]
        self.assertEqual([row["name"] for row in port], ["spec", "destination"])


if __name__ == "__main__":
    unittest.main()
