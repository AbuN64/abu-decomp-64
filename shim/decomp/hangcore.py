# SPDX-License-Identifier: GPL-3.0-or-later
"""Run-on boot with an automatic hang core at every watchdog event.

`python3 -I hangcore.py --out DIR -- BINARY ARGS...` boots BINARY (the walk build, N64C_RUN_ON=1 is set) under gdb,
e.g. `-- build/ragewars --no-dialog --rom ROM --capture out.ppm --retrace 180 --fb 284x222 --window 320x240`.
runon.c's watchdog calls break_hang() when no new first hit came for N64C_RUN_ON_HANG seconds; a breakpoint there
stops every thread before the breaker unwinds the spinning one, and DIR/hang-<n>.txt gets:
- every thread's backtrace (the guest frames are the original C functions);
- for each thread that is not parked in the scheduler (frame 0 outside libc), the innermost original function's
  disassembly around the pc, its registers, and each memory operand near the pc resolved to an address with the
  32-bit word it holds (what the spin waits on; guest RAM is identity-mapped at 0x80000000).
The boot then continues, so one run lists every hang with its core. The boot's own stdout/stderr go to DIR/boot.log.
Only this script's own gdb child is ever signalled (the boot's timeout is gdb's, by PID)."""
import os
import re
import subprocess
import sys

ABSOLUTE = re.compile(r"(?:^|[\s,]|%ds:)(0x[0-9a-f]+)(?=\s*(?:,|$))")
OPERAND = re.compile(r"(-?0x[0-9a-f]+|-?\d+)?\((%e[a-z]{2})?(?:,(%e[a-z]{2}),(\d))?\)")


def operands(text):
    """Memory operands of one AT&T instruction: [(disp, base, index, scale)] with register names without '%'."""
    out = []
    mnemonic, _, args = text.strip().partition(" ")
    if mnemonic.startswith(("j", "call", "lea", "nop")):
        return out  # branch targets and address arithmetic read no memory
    for absolute in ABSOLUTE.findall(args):
        out.append((int(absolute, 16), None, None, 1))
    for disp, base, index, scale in OPERAND.findall(args):
        out.append((int(disp, 0) if disp else 0, base[1:] if base else None, index[1:] if index else None,
                    int(scale) if scale else 1))
    return out


def address(op, regs):
    disp, base, index, scale = op
    total = disp + (regs.get(base, 0) if base else 0) + (regs.get(index, 0) * scale if index else 0)
    return total & 0xFFFFFFFF


def in_gdb(out_dir):
    import gdb  # noqa: provided by gdb

    state = {"n": 0}
    regnames = ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp", "eip")

    def frame_regs(frame):
        regs = {}
        for r in regnames:
            try:
                regs[r] = int(frame.read_register(r)) & 0xFFFFFFFF
            except gdb.error:
                pass
        return regs

    def core(text_out):
        inferior = gdb.selected_inferior()
        for thread in inferior.threads():
            thread.switch()
            frame = gdb.newest_frame()
            bt = gdb.execute("bt 16", to_string=True).rstrip()
            text_out.append(f"== thread {thread.num} lwp {thread.ptid[1]} {thread.name or ''}")
            text_out.append(bt)
            top = frame.name() or ""
            lib = gdb.solib_name(frame.pc()) or ""
            if "libc" in lib or "libpthread" in lib or top == "break_hang":
                continue  # parked in a host wait, or the watchdog itself
            # the innermost original function (a thunk or shim frame may sit above it)
            number = next((int(m.group(1)) for m in re.finditer(r"^#(\d+)\s+(?:0x[0-9a-f]+ in )?(func_\w+|rw_\w+)",
                                                                 bt, re.M)), 0)
            gdb.execute(f"frame {number}", to_string=True)
            f = gdb.selected_frame()
            regs = frame_regs(f)
            pc = f.pc()
            text_out.append(f"-- spinning candidate: {f.name()} pc {pc:#x} (frame #{number}; frame 0 {top})")
            text_out.append("   regs " + " ".join(f"{k}={v:#x}" for k, v in regs.items()))
            try:
                listing = gdb.execute(f"disassemble {pc:#x}", to_string=True).splitlines()
            except gdb.error:
                listing = []
            at = next((i for i, l in enumerate(listing) if l.startswith("=>")), None)
            if at is None:  # a caller frame: its pc is the return address
                at = next((i for i, l in enumerate(listing) if l.split()[:1] == [f"{pc:#010x}"] or f"{pc:#x} <" in l), 0)
            for l in listing[max(at - 14, 0):at + 10]:
                m = re.match(r"^(=>)?\s*(0x[0-9a-f]+) <[^>]*>:\s*(.*)$", l)
                if not m:
                    continue
                addr, asm = int(m.group(2), 16), m.group(3)
                line = f"   {'=>' if addr == pc else '  '} {addr:#x} {asm}"
                for op in operands(asm):
                    if op[1] in (None, "esp") and op[0] == 0:
                        continue
                    a = address(op, regs)
                    try:
                        word = int.from_bytes(bytes(inferior.read_memory(a, 4)), "little")
                        line += f"   [{a:#010x}]={word:#010x}"
                    except gdb.MemoryError:
                        line += f"   [{a:#010x}]=unmapped"
                text_out.append(line)

    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    for sig in ("SIGSEGV", "SIGBUS", "SIGFPE", "SIGURG", "SIGUSR1", "SIGUSR2", "SIG34", "SIG35", "SIGPIPE"):
        gdb.execute(f"handle {sig} nostop noprint pass")
    gdb.Breakpoint("break_hang")
    gdb.execute("run")
    # all-stop: each return from run/continue is a stop with every thread halted; at break_hang write the core
    while gdb.selected_inferior().pid:
        try:
            at_break = gdb.selected_frame().name() == "break_hang"
        except gdb.error:
            break
        if not at_break:
            break  # a stop this script does not own (an unhandled signal): leave it to the boot's own handling
        state["n"] += 1
        lines = [f"hangcore {state['n']}: watchdog event (runon.c break_hang)"]
        watchdog = gdb.selected_thread()
        try:
            core(lines)
        except Exception as exc:  # keep the boot going whatever the core reading hit
            lines.append(f"hangcore error: {exc!r}")
        watchdog.switch()
        path = os.path.join(out_dir, f"hang-{state['n']}.txt")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        sys.stderr.write(f"hangcore: {path}\n")
        gdb.execute("continue")


def main(argv):
    if "--" not in argv or argv[:1] != ["--out"]:
        sys.stderr.write("usage: hangcore.py --out DIR -- BINARY ARGS...\n")
        return 2
    out_dir = os.path.abspath(argv[1])
    boot = argv[argv.index("--") + 1:]
    os.makedirs(out_dir, exist_ok=True)
    env = dict(os.environ, N64C_RUN_ON=os.environ.get("N64C_RUN_ON", "1"), HANGCORE_OUT=out_dir)
    command = ["gdb", "-q", "-batch", "-nx", "-x", os.path.abspath(__file__), "--args", *boot]
    seconds = int(os.environ.get("N64C_RUN_ON_LIMIT", "300")) + 60
    with open(os.path.join(out_dir, "boot.log"), "w") as log:
        try:  # the timeout kills only this gdb child (by PID); the boot ends with it
            return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=seconds).returncode
        except subprocess.TimeoutExpired:
            log.write(f"hangcore: gdb ran past {seconds} s; killed\n")
            return 124


if __name__ == "__main__":
    try:
        import gdb  # noqa: F401  (present only when gdb sources this file)
    except ImportError:
        sys.exit(main(sys.argv[1:]))
    in_gdb(os.environ["HANGCORE_OUT"])
