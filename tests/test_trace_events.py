"""`decomp tas play --trace-events FUNC[/N],...`: the gdb bridge turns the list into dprintf lines and writes their
output to RUN/events.txt (TAS-COVER-5/6 wrote gdb init files and grepped MI noise by hand)."""

import asyncio
import json

from abu_decomp.supervisor import gdb


def test_event_commands_read_arguments_the_way_the_abi_passes_them():
    assert gdb.event_commands("Gfx_DrawRect/2, Frame_Step", 4) == [
        'dprintf *Gfx_DrawRect,"EV|Gfx_DrawRect|%d|%d\\n", *(int*)($esp+4), *(int*)($esp+8)',
        'dprintf *Frame_Step,"EV|Frame_Step\\n"',
    ]
    assert gdb.event_commands("F/1", 8) == ['dprintf *F,"EV|F|%d\\n", (long)$rdi']


def test_pointer_size_reads_the_elf_class(tmp_path):
    (tmp_path / "a32").write_bytes(b"\x7fELF\x01")
    (tmp_path / "a64").write_bytes(b"\x7fELF\x02")
    assert gdb.pointer_size(tmp_path / "a32") == 4 and gdb.pointer_size(tmp_path / "a64") == 8
    assert gdb.pointer_size(tmp_path / "none") == 8


def test_event_lines_go_to_events_txt_and_nowhere_else(tmp_path, capsys):
    cls = next(v for v in vars(gdb).values() if isinstance(v, type) and hasattr(v, "read_output"))
    bridge = cls.__new__(cls)
    bridge.run_dir = tmp_path / "run"
    bridge.stack_mode = None
    bridge.queue = asyncio.Queue()
    lines = ["~" + json.dumps("EV|F|1|2\n"), "~" + json.dumps("other\n"), ""]

    class Out:
        async def readline(self):
            return (lines.pop(0) + "\n").encode() if lines[0] else b""

    bridge.proc = type("P", (), {"stdout": Out()})()
    asyncio.run(bridge.read_output())
    assert (tmp_path / "run/events.txt").read_text() == "EV|F|1|2\n"


def test_a_fault_dumps_guest_ram_trying_the_shim_size_first(tmp_path):
    cls = next(v for v in vars(gdb).values() if isinstance(v, type) and hasattr(v, "dump_ram"))
    bridge = cls.__new__(cls)
    bridge.run_dir = tmp_path
    sent = []

    async def cli(command):
        sent.append(command)
        if "0x400000" in command:
            (tmp_path / gdb.RAM_FAULT).write_bytes(b"ram")
            return "5^done"
        return '5^error,msg="No symbol"'

    bridge.cli = cli
    assert asyncio.run(bridge.dump_ram())
    assert len(sent) == 3 and "ram_size" in sent[0] and sent[0].startswith("dump binary memory")


def test_events_carry_the_poll_when_the_shim_counter_reads():
    got = gdb.event_commands("F/1", 4, gdb.POLL)
    assert got == ['dprintf *F,"EV|F@%ld|%d\\n", (long)' + gdb.POLL + ", *(int*)($esp+4)"]
