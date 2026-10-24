import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import abu_decomp

PACKAGE = Path(abu_decomp.__file__).parent


class Client:
    def __init__(self, source: Path):
        environment = {k: v for k, v in os.environ.items() if k not in ("DECOMP_PROFILE", "DECOMP_TAS_PROFILE")}
        # a seat's own profile link must not answer for the missing --profile these tests expect to refuse
        environment.update(PYTHONPATH=str(source), XDG_CONFIG_HOME=str(source / "no-config"), HOME=str(source))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "abu_decomp", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=environment,
            bufsize=0,  # unbuffered, so select() sees every line the server has written
        )
        self.next = 0

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next += 1
            message["id"] = self.next
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        return None if notify else self.read()

    def read(self, timeout=20.0):
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("no MCP message")
        return json.loads(self.process.stdout.readline())

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close()


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        shutil.copytree(PACKAGE, self.source / "abu_decomp", ignore=shutil.ignore_patterns("__pycache__"))
        self.client = Client(self.source)
        self.addCleanup(self.client.close)

    def test_round_trip_and_live_cli_edit_without_restart(self):
        client = self.client
        hello = client.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(hello["result"]["capabilities"]["tools"], {"listChanged": True})
        client.send("notifications/initialized", notify=True)
        listed = {tool["name"]: tool for tool in client.send("tools/list")["result"]["tools"]}
        self.assertIn("decomp_run", listed)
        self.assertIn("decomp_dependencies", listed)
        self.assertNotIn("decomp_mcp", listed)
        called = client.send("tools/call", {"name": "decomp_run", "arguments": {"args": ["describe"]}})["result"]
        self.assertFalse(called["isError"])
        self.assertTrue(called["structuredContent"]["ok"])

        cli = self.source / "abu_decomp" / "cli.py"
        cli.write_text(cli.read_text().replace("list every command and its arguments as JSON", "edited help"))
        called = client.send("tools/call", {"name": "decomp_describe", "arguments": {"args": []}})["result"]
        helps = {row["name"]: row["help"] for row in called["structuredContent"]["result"]["commands"]}
        self.assertEqual(helps["describe"], "edited help")
        self.assertEqual(client.read()["method"], "notifications/tools/list_changed")
        listed = {tool["name"]: tool for tool in client.send("tools/list")["result"]["tools"]}
        self.assertTrue(listed["decomp_describe"]["description"].startswith("edited help"))

    def test_failures_are_tool_errors_and_unknown_methods_are_rpc_errors(self):
        client = self.client
        client.send("initialize")
        failed = client.send("tools/call", {"name": "decomp_profile", "arguments": {"args": []}})["result"]
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["structuredContent"]["findings"][0]["key"], "config.missing")
        self.assertEqual(client.send("tools/call", {"name": "decomp_mcp", "arguments": {}})["error"]["code"], -32603)
        self.assertEqual(client.send("resources/list")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
