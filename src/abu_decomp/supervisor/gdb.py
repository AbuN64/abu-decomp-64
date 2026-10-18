#!/usr/bin/env python3
"""GDB MI bridge: hold a real inferior at a configured failure boundary."""

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from contextlib import suppress
from pathlib import Path


def decode_stream(record):
    try:
        return json.loads(record[1:])
    except (ValueError, TypeError):
        return record[1:].strip().strip('"')


def exit_code(record):
    match = re.search(r"exit-code=\"([^\"]+)\"", record)
    if not match:
        return None
    value = match.group(1)
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 8)
        except ValueError:
            return None


class Bridge:
    def __init__(self, argv, *, gdb_path, library_dir, breakpoint, reason_expression, run_dir):
        self.argv = argv
        self.gdb_path = Path(gdb_path)
        self.library_dir = Path(library_dir) if library_dir else None
        self.breakpoint = breakpoint
        self.reason_expression = reason_expression
        self.run_dir = Path(run_dir)
        self.proc = None
        self.queue = asyncio.Queue()
        self.backlog = []
        self.token = 0
        self.stack_lines = []
        self.full_stack_lines = []
        self.stack_mode = None
        self.stopped = False
        self.signal_name = None
        self.inferior_exit = None
        self.command_reader = None

    async def read_output(self):
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                await self.queue.put(("eof", ""))
                return
            record = raw.decode(errors="replace").rstrip("\r\n")
            if record.startswith(("~", "@", "&")):
                value = decode_stream(record)
                if record[0] == "@":
                    print(value, end="" if value.endswith("\n") else "\n", flush=True)
                elif self.stack_mode == "visible" and record[0] == "~":
                    for line in value.splitlines():
                        if line:
                            print("[C stack] " + line, flush=True)
                            self.stack_lines.append(line)
                elif self.stack_mode == "artifact" and record[0] == "~":
                    self.full_stack_lines.extend(line for line in value.splitlines() if line)
                elif record[0] == "&" and "error" in value.lower():
                    print("[gdb] " + value.strip(), flush=True)
                continue
            if record.startswith("*stopped"):
                await self.queue.put(("stopped", record))
            elif record.startswith("=thread-group-exited"):
                await self.queue.put(("group-exit", record))
            elif "^" in record and record.split("^", 1)[0].isdigit():
                await self.queue.put(("result", record))
            elif record and record.strip() != "(gdb)" and not record.startswith(("=", "*", "+")):
                # Some inferior output bypasses MI stream quoting.
                print(record, flush=True)

    async def send(self, body):
        self.token += 1
        token = self.token
        self.proc.stdin.write((str(token) + body + "\n").encode())
        await self.proc.stdin.drain()
        return token

    async def next_event(self):
        if self.backlog:
            return self.backlog.pop(0)
        return await self.queue.get()

    async def result(self, token):
        held = self.backlog
        self.backlog = []
        while True:
            kind, record = await self.queue.get()
            if kind == "result" and record.startswith(str(token) + "^"):
                self.backlog = held + self.backlog
                return record
            held.append((kind, record))

    async def cli(self, command):
        token = await self.send("-interpreter-exec console " + json.dumps(command))
        return await self.result(token)

    async def stack(self, label):
        await self.cli("set print frame-arguments none")
        await self.cli("set filename-display basename")
        print("C_STACK_BEGIN " + label, flush=True)
        self.stack_lines = []
        self.stack_mode = "visible"
        await self.cli("bt 30")
        self.stack_mode = None
        if self.run_dir:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            path = self.run_dir / ("failure-stack.txt" if label == self.breakpoint else "signal-stack.txt")
            path.write_text("\n".join(self.stack_lines) + "\n")
            print("debug-stack: " + str(path), flush=True)
            await self.cli("set filename-display absolute")
            await self.cli("set print frame-arguments all")
            self.full_stack_lines = []
            self.stack_mode = "artifact"
            await self.cli("bt 30")
            self.stack_mode = None
            full = self.run_dir / ("failure-stack-full.txt" if label == self.breakpoint else "signal-stack-full.txt")
            full.write_text("\n".join(self.full_stack_lines) + "\n")
        print("C_STACK_END " + label, flush=True)

    async def run(self):
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        self.command_reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self.command_reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        env = os.environ.copy()
        if self.library_dir:
            env["LD_LIBRARY_PATH"] = str(self.library_dir) + (
                ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
            )
        env["DEBUGINFOD_URLS"] = ""
        self.proc = await asyncio.create_subprocess_exec(
            str(self.gdb_path),
            "--interpreter=mi2",
            "--quiet",
            "--nx",
            "--args",
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        reader = asyncio.create_task(self.read_output())
        try:
            for body in ("-gdb-set confirm off", "-gdb-set pagination off", "-break-insert " + self.breakpoint):
                token = await self.send(body)
                response = await self.result(token)
                if response.startswith(str(token) + "^error"):
                    print("[gdb] setup failed: " + response, flush=True)
                    return 125
            token = await self.send("-exec-run")
            response = await self.result(token)
            if response.startswith(str(token) + "^error"):
                print("[gdb] run failed: " + response, flush=True)
                return 125
            while True:
                kind, record = await self.next_event()
                if kind == "stopped":
                    if 'reason="breakpoint-hit"' in record and ('func="' + self.breakpoint + '"') in record:
                        self.stopped = True
                        print("debug-stop: " + self.breakpoint, flush=True)
                        token = await self.send("-data-evaluate-expression " + self.reason_expression)
                        reason_result = await self.result(token)
                        if "value=" in reason_result:
                            value_text = reason_result.split("value=", 1)[1]
                            try:
                                value, _ = json.JSONDecoder().raw_decode(value_text)
                                reason = value.split('"', 1)[1].rsplit('"', 1)[0] if '"' in value else value
                                print("debug-reason: " + reason, flush=True)
                            except (ValueError, IndexError):
                                pass
                        await self.stack(self.breakpoint)
                        while True:
                            raw_instruction = await self.command_reader.readline()
                            instruction = raw_instruction.decode(errors="replace")
                            if instruction == "":
                                print("[gdb] supervisor pipe closed while inferior stopped", flush=True)
                                return 125
                            if instruction.strip() == "continue":
                                break
                        token = await self.send("-exec-continue")
                        response = await self.result(token)
                        if response.startswith(str(token) + "^error"):
                            print("[gdb] continue failed: " + response, flush=True)
                            return 125
                    elif 'reason="signal-received"' in record:
                        name = re.search(r"signal-name=\"([^\"]+)\"", record)
                        self.signal_name = name.group(1) if name else "unknown"
                        print("debug-signal: " + self.signal_name, flush=True)
                        await self.stack(self.signal_name)
                        return 139 if self.signal_name == "SIGSEGV" else 128
                    elif 'reason="exited' in record:
                        self.inferior_exit = exit_code(record)
                        if self.inferior_exit is None and "exited-normally" in record:
                            self.inferior_exit = 0
                        return self.inferior_exit if self.inferior_exit is not None else 125
                elif kind == "group-exit":
                    self.inferior_exit = exit_code(record)
                    return self.inferior_exit if self.inferior_exit is not None else 0
                elif kind == "eof":
                    return 125
        finally:
            reader.cancel()
            if self.proc and self.proc.returncode is None:
                try:
                    self.proc.stdin.write(b"-gdb-exit\n")
                    await self.proc.stdin.drain()
                    await asyncio.wait_for(self.proc.wait(), 2)
                except (TimeoutError, OSError):
                    with suppress(ProcessLookupError):
                        os.killpg(self.proc.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(self.proc.wait(), 2)
                    except TimeoutError:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                        await self.proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gdb", required=True)
    parser.add_argument("--library-dir", default="")
    parser.add_argument("--breakpoint", required=True)
    parser.add_argument("--reason-expression", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv or not Path(argv[0]).is_file():
        parser.error("pass an existing executable after --")
    return asyncio.run(
        Bridge(
            argv,
            gdb_path=args.gdb,
            library_dir=args.library_dir,
            breakpoint=args.breakpoint,
            reason_expression=args.reason_expression,
            run_dir=args.run_dir,
        ).run()
    )


if __name__ == "__main__":
    raise SystemExit(main())
