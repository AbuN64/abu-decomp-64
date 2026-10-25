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
