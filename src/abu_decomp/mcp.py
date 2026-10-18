"""`decomp mcp`: a thin, stable MCP stdio shim over the `decomp` CLI.

The shim holds no command logic. Every tools/list derives the tools from a fresh `decomp describe`, and every
tools/call runs the current CLI in a fresh interpreter, so an edit to decomp's code is live on the next call
with no reinstall and no client restart. When the derived tool list changes, the shim sends
notifications/tools/list_changed. Only an edit to this file needs the client to restart the server.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Any, BinaryIO

from abu_decomp import __version__

PROTOCOL = "2025-06-18"
RUN = "decomp_run"
# Commands that are not useful as tools from inside the MCP server itself.
_HIDDEN = frozenset({"mcp"})


def _cli(prefix: Sequence[str], args: Sequence[str]) -> tuple[int, str, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "abu_decomp", *prefix, *args], capture_output=True, text=True, check=False
    )
    return completed.returncode, completed.stdout, completed.stderr


def _args_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"args": {"type": "array", "items": {"type": "string"}, "description": description}},
        "required": ["args"],
        "additionalProperties": False,
    }


def tools(prefix: Sequence[str]) -> list[dict[str, Any]]:
    """One tool per command group plus decomp_run, derived from the CLI's own parser at call time."""
    code, out, err = _cli(prefix, ["describe"])
    try:
        commands = json.loads(out)["result"]["commands"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"decomp describe failed (exit {code}): {err.strip() or out.strip()}") from exc
    listed = [
        {
            "name": RUN,
            "description": "Run any decomp command line; args exclude the program name. One JSON envelope back.",
            "inputSchema": _args_schema('decomp arguments, for example ["dependencies"]'),
        }
    ]
    for command in commands:
        if command["name"] in _HIDDEN:
            continue
        listed.append(
            {
                "name": "decomp_" + command["name"],
                "description": f"{command['help']}. {command['usage']}",
                "inputSchema": _args_schema(f"arguments after `decomp {command['name']}`"),
                "x-arguments": command["arguments"],
            }
        )
    return listed


def _fingerprint(listed: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(listed, sort_keys=True).encode()).hexdigest()


class Server:
    def __init__(self, prefix: Sequence[str]):
        self.prefix = list(prefix)
        self.listed: str | None = None

    def handle(self, message: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """One request in; its response (None for notifications) and any notifications to send after it."""
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        notes: list[dict[str, Any]] = []
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL),
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "decomp", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                listed = tools(self.prefix)
                self.listed = _fingerprint(listed)
                result = {"tools": listed}
            elif method == "tools/call":
                result = self.call(str(params.get("name", "")), params.get("arguments") or {})
                current = _fingerprint(tools(self.prefix))
                if self.listed is not None and current != self.listed:
                    self.listed = current
                    notes.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            elif ident is None:
                return None, notes
            else:
                return _error(ident, -32601, f"method not found: {method}"), notes
        except Exception as exc:
            return (None if ident is None else _error(ident, -32603, str(exc))), notes
        return (None if ident is None else {"jsonrpc": "2.0", "id": ident, "result": result}), notes

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = arguments.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("args must be a list of strings")
        if name == RUN:
            argv = args
        elif name.startswith("decomp_") and name.removeprefix("decomp_") not in _HIDDEN:
            argv = [name.removeprefix("decomp_"), *args]
        else:
            raise ValueError(f"unknown tool: {name}")
        code, out, err = _cli(self.prefix, argv)
        result: dict[str, Any] = {"content": [{"type": "text", "text": out or err}], "isError": code != 0}
        with contextlib.suppress(ValueError):
            result["structuredContent"] = json.loads(out)
        return result


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def serve(reader: BinaryIO, writer: BinaryIO, *, prefix: Sequence[str] = ()) -> int:
    """Newline-delimited JSON-RPC over stdio until the client closes stdin."""
    server = Server(prefix)

    def send(value: dict[str, Any]) -> None:
        writer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        writer.flush()

    for raw in reader:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            send(_error(None, -32700, "parse error"))
            continue
        response, notes = server.handle(message)
        if response is not None:
            send(response)
        for note in notes:
            send(note)
    return 0
