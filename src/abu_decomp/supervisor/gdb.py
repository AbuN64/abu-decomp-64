#!/usr/bin/env python3
"""GDB MI bridge: hold a real inferior at a configured failure boundary."""

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import sys
from contextlib import suppress
from pathlib import Path

from abu_decomp import procguard


def decode_stream(record):
    try:
        return json.loads(record[1:])
    except (ValueError, TypeError):
        return record[1:].strip().strip('"')


_MI_ASYNC = re.compile(r"[=*+][a-z][a-z-]*(,|$)")


RAM_FAULT = "ram-fault.bin"


POLL = "*(long*)&polls"  # the shim's poll counter (shim/n64c_tas.c; a static: read through its address)


def event_commands(spec, pointer_bytes, poll=None):
    """gdb `dprintf` lines for N64C_TRACE_EVENTS (`FUNC[/N],...`): at each FUNC's first instruction print
    `EV|FUNC|a0|..|aN-1` (its first N integer arguments, read the way the ABI passes them at entry: the stack above the
    return address for a 32-bit port, rdi rsi rdx rcx r8 r9 for a 64-bit one). The bridge appends these lines to
    RUN/events.txt in the order the port hit them (TAS-COVER-5/6 wrote these by hand; an argument named by a symbol
    the port lacks stalls gdb, a register or stack read never does). With POLL (an expression the bridge proved
    readable), each line is `EV|FUNC@poll|...`, so calls count per poll against the original's (TAS-COVER-4)."""
    regs = ("$rdi", "$rsi", "$rdx", "$rcx", "$r8", "$r9")
    out = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        name, _, count = item.partition("/")
        n = min(int(count or 0), 6)
        if pointer_bytes == 8:
            args = [f"(long){regs[i]}" for i in range(n)]
        else:
            args = [f"*(int*)($esp+{4 + 4 * i})" for i in range(n)]
        if poll:
            fmt = "EV|" + name + "@%ld" + "|%d" * n + "\\n"
            args = [f"(long){poll}", *args]
        else:
            fmt = "EV|" + name + "|%d" * n + "\\n"
        out.append(f'dprintf *{name},"{fmt}"' + "".join(", " + a for a in args))
    return out


def pointer_size(path):
    """4 or 8 from an ELF file's class byte, else 8."""
    try:
        with open(path, "rb") as f:
            head = f.read(5)
        return 4 if head[:4] == b"\x7fELF" and head[4] == 1 else 8
    except OSError:
        return 8


def is_mi_async(record):
    """An MI async record (`=thread-created,...`, `*running,...`, `+download,...`): a class word right after the sign.
    Inferior output that only starts with one of those signs is not one: AddressSanitizer opens every report with
    `==PID==ERROR: AddressSanitizer: ...` and a `=====` rule; dropping them hid every ASan report from the
    findings."""
    return bool(_MI_ASYNC.match(record))


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
        self.watch_hits = 0
        self.fpe_hits = 0

    async def read_output(self):
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                await self.queue.put(("eof", ""))
                return
            record = raw.decode(errors="replace").rstrip("\r\n")
            if record.startswith(("~", "@", "&")):
                value = decode_stream(record)
                if record[0] == "~" and value.startswith("EV|"):
                    self.run_dir.mkdir(parents=True, exist_ok=True)
                    with (self.run_dir / "events.txt").open("a") as out:
                        out.write(value if value.endswith("\n") else value + "\n")
                    continue
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
            elif record and record.strip() != "(gdb)" and not is_mi_async(record):
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

    async def dump_ram(self):
        """Guest RAM at a host fault as RUN/ram-fault.bin, read by gdb without calling into the stopped port: the shim
        maps guest RDRAM at host 0x80000000 (`n64c_memory_ram`), its size in the shim's `ram_size`, else the 4 MB
        base. A crash whose batch is built at draw time can then be read like a poll dump (TAS-COVER-3)."""
        out = self.run_dir / RAM_FAULT
        for size in ("*(unsigned int*)&ram_size", "'n64c_memory.c'::ram_size", "0x400000"):
            response = await self.cli(f"dump binary memory {out} 0x80000000 0x80000000+{size}")
            if "^done" in (response or "") and out.is_file() and out.stat().st_size:
                return True
        return False

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
            # Every thread: the stop is raised on the frame loop's thread, the game's own C frames are on the
            # game threads.
            await self.cli("set print frame-arguments scalars")
            await self.cli("set filename-display basename")
            self.full_stack_lines = []
            self.stack_mode = "artifact"
            await self.cli("thread apply all bt 30")
            self.stack_mode = None
            threads = self.run_dir / ("failure-threads.txt" if label == self.breakpoint else "signal-threads.txt")
            threads.write_text("\n".join(self.full_stack_lines) + "\n")
            if label != self.breakpoint:
                # Read the stopped thread without calling into an inferior with a pending FP trap.
                self.full_stack_lines = []
                self.stack_mode = "artifact"
                for command in (
                    "frame 0",
                    "info registers eip ebp esp mxcsr fctrl fstat",
                    "x/i $pc",
                    'printf "pointer_bytes=%d\\n", sizeof(void*)',
                    "x/16wx $ebp+8",
                ):
                    await self.cli(command)
                self.stack_mode = None
                (self.run_dir / "signal-context.txt").write_text("\n".join(self.full_stack_lines) + "\n")
                await self.dump_ram()
                # Lane-supplied reads at the fault (N64C_FAULT_GDB = a file of gdb commands, one per line): the
                # sprite, glyph or batch the faulting frame holds, written beside the stacks as signal-extra.txt.
                script = os.environ.get("N64C_FAULT_GDB")
                if script and Path(script).is_file():
                    self.full_stack_lines = []
                    self.stack_mode = "artifact"
                    for command in Path(script).read_text().splitlines():
                        if command.strip() and not command.startswith("#"):
                            self.full_stack_lines.append("(gdb) " + command)
                            try:
                                await self.cli(command)
                            except Exception as error:  # a bad command must not lose the rest or the stop report
                                self.full_stack_lines.append("(error) " + repr(error))
                    self.stack_mode = None
                    (self.run_dir / "signal-extra.txt").write_text("\n".join(self.full_stack_lines) + "\n")
        print("C_STACK_END " + label, flush=True)

    @staticmethod
    def wrapper_args() -> list[str]:
        """The game gdb launches dies with gdb: PDEATHSIG survives the exec, so no tracee outlives it."""
        if not shutil.which("setpriv"):
            return []
        return ["-iex", "set exec-wrapper " + " ".join(procguard.wrap([]))]

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
            *self.wrapper_args(),
            "--args",
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            **procguard.owned(),
        )
        reader = asyncio.create_task(self.read_output())
        try:
            for body in ("-gdb-set confirm off", "-gdb-set pagination off", "-break-insert " + self.breakpoint):
                token = await self.send(body)
                response = await self.result(token)
                if response.startswith(str(token) + "^error"):
                    print("[gdb] setup failed: " + response, flush=True)
                    return 125
            # The shim takes SIGSEGV (guest TLB-miss service) and SIGUSR2 (resume) on purpose and handles them
            # itself. Without this gdb stops on each one and the bridge reports a crash (tile stuck at 75/932).
            # A genuine host fault is named at the breakpoint by the shim (host-fault-<addr>) while traced.
            handles = ["handle SIGSEGV nostop noprint pass", "handle SIGUSR2 nostop noprint pass"]
            if os.environ.get("N64C_GDB_FPE_TOLERATE"):
                handles.append("handle SIGFPE stop print nopass")
            for body in handles:
                await self.cli(body)
            # Lane-supplied setup before the run (N64C_GDB_INIT = a file of gdb commands, e.g. a `watch`): each
            # watchpoint hit appends its stack to watch.txt in the run folder and the run goes on (200 hits at most).
            # The file is sourced whole: gdb reads a `commands ... end` block from it, where the same lines sent one
            # by one over MI left gdb waiting at its ">" prompt for the block's end (PORT-FIX-O1 FRICTION, 10-25).
            init = os.environ.get("N64C_GDB_INIT")
            if init and Path(init).is_file():
                response = await self.cli(f"source {Path(init).resolve()}")
                if "^error" in response:
                    print("[gdb] N64C_GDB_INIT refused: " + response[-200:], flush=True)
            events = os.environ.get("N64C_TRACE_EVENTS")
            if events:
                await self.cli("set dprintf-style gdb")
                probe = await self.cli(f"output (long){POLL}")  # a missing symbol must not reach a dprintf: it stalls
                poll = POLL if "^done" in probe else None
                for command in event_commands(events, pointer_size(self.argv[0]), poll):
                    response = await self.cli(command)
                    if "^error" in response:
                        print("[gdb] trace event refused: " + command + ": " + response[-160:], flush=True)
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
                        reason = ""
                        if "value=" in reason_result:
                            value_text = reason_result.split("value=", 1)[1]
                            try:
                                value, _ = json.JSONDecoder().raw_decode(value_text)
                                reason = value.split('"', 1)[1].rsplit('"', 1)[0] if '"' in value else value
                                print("debug-reason: " + reason, flush=True)
                            except (ValueError, IndexError):
                                pass
                        await self.stack(self.breakpoint)
                        if reason.startswith("host-fault"):  # a crash the shim named: its RAM too
                            await self.dump_ram()
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
                    elif 'reason="watchpoint-trigger"' in record and self.watch_hits < 200:
                        self.watch_hits += 1
                        self.full_stack_lines = [f"watch hit {self.watch_hits}: {record[:400]}"]
                        self.stack_mode = "artifact"
                        await self.cli("bt 8")
                        self.stack_mode = None
                        self.run_dir.mkdir(parents=True, exist_ok=True)
                        with (self.run_dir / "watch.txt").open("a") as out:
                            out.write("\n".join(self.full_stack_lines) + "\n")
                        token = await self.send("-exec-continue")
                        await self.result(token)
                    elif (
                        'reason="signal-received"' in record
                        and 'signal-name="SIGFPE"' in record
                        and os.environ.get("N64C_GDB_FPE_TOLERATE")
                        and self.fpe_hits < 100000
                    ):
                        # Experiment mode (a lane asks what the original would do if this divide did not trap): log the
                        # faulting frame, mask the trap for the one instruction, restore the trap and go on.
                        self.fpe_hits += 1
                        if self.fpe_hits <= 20:
                            self.full_stack_lines = []
                            self.stack_mode = "artifact"
                            await self.cli("bt 6")
                            self.stack_mode = None
                            self.run_dir.mkdir(parents=True, exist_ok=True)
                            with (self.run_dir / "fpe-tolerated.txt").open("a") as out:
                                out.write(f"fpe {self.fpe_hits}\n" + "\n".join(self.full_stack_lines) + "\n")
                        await self.cli("set $mxcsr = $mxcsr | 0x200")
                        token = await self.send("-exec-step-instruction")
                        await self.result(token)
                        while True:
                            kind, record = await self.next_event()
                            if kind == "stopped" or kind == "eof":
                                break
                        await self.cli("set $mxcsr = ($mxcsr & ~0x200) & ~0x3f")
                        token = await self.send("-exec-continue")
                        await self.result(token)
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
