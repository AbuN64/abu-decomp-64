"""Live tails behind the TAS page's tabs: AI orchestrator transcript, port log, decompiled-function events."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CAP = 300
_WINDOW = 2_000_000
FUNCTION_KINDS = frozenset({"PROBE", "MATCH", "LAND", "RUN-FAIL", "HANDOFF"})
_GAME_THREAD = re.compile(r"n64c_game|n64c_boot_stopped")


def _last_lines(path: Path, window: int = _WINDOW) -> list[str]:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(size - window, 0))
        data = stream.read()
    lines = data.decode(errors="replace").splitlines()
    return lines[1:] if size > window and lines else lines


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def ai_lines(folder: Path) -> list[str]:
    """Readable lines of the newest *.jsonl Claude Code transcript: assistant text and one line per tool call."""
    files = [p for p in folder.glob("*.jsonl") if p.is_file()] if folder.is_dir() else []
    if not files:
        return []
    out: list[str] = []
    for raw in _last_lines(max(files, key=lambda p: p.stat().st_mtime)):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
        content = message.get("content") if isinstance(message, dict) else None
        stamp = str(row.get("timestamp", ""))[11:19] if isinstance(row, dict) else ""
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(f"{stamp} {_one_line(block['text'], 300)}")
            elif block.get("type") == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                detail = args.get("description") or args.get("command") or args.get("file_path") or args.get("pattern")
                out.append(f"{stamp} > {block.get('name')}: {_one_line(str(detail or ''), 120)}")
    return out[-CAP:]


def game_threads(text: str) -> list[str]:
    """The thread blocks of a gdb `thread apply all bt` dump that run the game (scheduler or boot stop frames)."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("Thread "):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    return [line for block in blocks if _GAME_THREAD.search("\n".join(block)) for line in block]


def port_lines(log: Path | None, run_dir: Path | None, stopped: bool) -> list[str]:
    lines = _last_lines(log) if log and log.is_file() else []
    if stopped and run_dir and run_dir.is_dir():
        for name in ("failure-stack.txt", "failure-threads.txt"):
            path = run_dir / name
            if path.is_file():
                text = path.read_text(errors="replace")
                lines += [f"--- {name}", *(game_threads(text) if name == "failure-threads.txt" else text.splitlines())]
    return lines[-CAP:] if not stopped else lines[-(CAP + 200) :]


def function_lines(path: Path) -> list[str]:
    """One line per PROBE/MATCH/LAND/RUN-FAIL/HANDOFF event of the seat feed: time, kind, subject, verdict/message."""
    from abu_decomp import events

    if not path.is_file():
        return []
    out: list[str] = []
    for raw in _last_lines(path, 4_000_000):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = events.writer().canonical_kind(row)
        if kind not in FUNCTION_KINDS:
            continue
        payload: dict[str, Any] = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        subject = row.get("subject") or payload.get("function") or payload.get("op") or ""
        note = payload.get("verdict") or row.get("message") or ""
        out.append(f"{str(row.get('time', ''))[11:19]} {kind} {subject} {_one_line(str(note), 160)}")
    return out[-CAP:]
