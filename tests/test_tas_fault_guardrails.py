"""Signal stops must carry enough evidence to route a crash without reading gdb by hand."""

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from abu_decomp import tas, tas_buckets, tas_fault, tas_findings, tas_symbolize
from abu_decomp.cli import main
from abu_decomp.supervisor.gdb import Bridge


class FaultGuardrails(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fn = "func_80417B20_de"
        self.profile = SimpleNamespace(source_file=self.root / "profile.json")

    def test_signal_stack_and_context_feed_the_same_symbolizer_and_triage(self):
        (self.root / "signal-stack.txt").write_text(
            f"#0  0x08246de0 in {self.fn} ()\n#1  0x082470b3 in Gfx_DrawRect ()\n"
        )
        (self.root / "signal-context.txt").write_text(
            "eip 0x08246de0\nebp 0xff001000\nmxcsr 0x1d84 [ ZE ]\n"
            "fctrl 0x37b\nfstat 0x0\npointer_bytes=4\n"
            "0xff001008: 0x00000002 0x3f800000 0x00000000 0x12345678\n"
        )
        instruction = f"0x8246de0 <{self.fn}+0x1290>: divss 0x14(%ebp),%xmm0"
        with (
            mock.patch.object(
                tas_symbolize,
                "resolve",
                side_effect=lambda port, pc: (self.fn, 0x1290) if pc == 0x8246DE0 else ("Gfx_DrawRect", 0x43),
            ),
            mock.patch.object(tas_symbolize, "instruction_of", return_value=instruction),
            mock.patch.object(tas_symbolize, "line_of", return_value="src/rect.CPP:255"),
            mock.patch.object(tas_symbolize, "frame_pointer", return_value=True),
            mock.patch("abu_decomp.tas_triage._project", return_value=None),
            mock.patch("abu_decomp.arity.notes", return_value=[]),
        ):
            fault = tas_fault.capture(self.root, "/port")
            result = {"movie": "crasher", "id": "run1", "outcome": "crash", "subject": "SIGFPE", "fault": fault}
            result.update(tas_symbolize.claim(self.profile, result, health={}))
        self.assertEqual(tas.fn_of(result), self.fn)
        self.assertEqual(result["kind"], "guest FP exception")
        self.assertEqual(result["source"], "src/rect.CPP:255")
        self.assertEqual(result["instruction"], instruction)
        self.assertEqual(fault["arguments"]["words"], [2, 0x3F800000, 0, 0x12345678])
        self.assertIn("guest FP exception: divide-by-zero, enabled by FCSR bit 10", result["claim"])
        self.assertEqual(result["fcsr"]["cause_bits"], 1 << 15)
        buckets = tas_buckets.build(tas_findings.from_run(result, self.root), str(self.profile.source_file))
        rendered = tas_buckets.render(buckets)
        self.assertIn("src/rect.CPP:255", rendered)
        self.assertIn("divss", rendered)
        self.assertIn("FCSR bit 10", rendered)

    def test_other_signals_get_the_guest_owner_and_missing_debug_info_is_explicit(self):
        (self.root / "src").mkdir()
        (self.root / "src/f.c").write_text("void DrawRectangle(void)\n{\n}\n")
        for sig in ("SIGILL", "SIGBUS", "SIGSEGV", "SIGABRT"):
            with (
                self.subTest(signal=sig),
                mock.patch("abu_decomp.tas_triage._project", return_value=self.root),
                mock.patch("abu_decomp.arity.notes", return_value=[]),
            ):
                result = {
                    "outcome": "crash",
                    "subject": sig,
                    "fault": {"eip": "0x10", "at": "DrawRectangle+0x1", "instruction": "ud2"},
                }
                result.update(tas_symbolize.claim(self.profile, result, health={}))
                self.assertEqual(tas.fn_of(result), "DrawRectangle")
                self.assertEqual(result["source"], "src/f.c:1")
                self.assertIn("function definition", result["source_precision"])
                self.assertEqual(result["kind"], "host fault")

    def test_only_enabled_native_causes_become_guest_exceptions(self):
        self.assertIsNone(tas_fault.fp_exception({}, "divss"))
        self.assertEqual(tas_fault.fp_exception({"mxcsr": 0x1F84}, "divss")["exceptions"], [])
        for native, guest, name in (
            (5, 0, "inexact"),
            (4, 1, "underflow"),
            (3, 2, "overflow"),
            (2, 3, "divide-by-zero"),
            (0, 4, "invalid operation"),
        ):
            got = tas_fault.fp_exception({"mxcsr": (0x1F80 & ~(1 << (7 + native))) | (1 << native)}, "divss")
            self.assertEqual(got["exceptions"], [{"exception": name, "enable_bit": guest + 7, "cause_bit": guest + 12}])
        got = tas_fault.fp_exception({"fctrl": 0x37B, "fstat": 4, "mxcsr": 0x1F80}, "fdivs 0x10(%ebp)")
        self.assertEqual(got["exceptions"][0]["enable_bit"], 10)
        self.assertEqual(tas_fault.fp_exception({"mxcsr": 0x1E82}, "divss")["exceptions"], [])

    def test_no_frame_pointer_means_no_claimed_crash_arguments(self):
        (self.root / "signal-stack.txt").write_text(f"#0  0x10 in {self.fn} ()\n")
        (self.root / "signal-context.txt").write_text("pointer_bytes=4\n0xff00: 0x00000001\n")
        with (
            mock.patch.object(tas_symbolize, "resolve", return_value=(self.fn, 0)),
            mock.patch.object(tas_symbolize, "frame_pointer", return_value=False),
        ):
            self.assertNotIn("arguments", tas_fault.capture(self.root, "/missing-port"))

    def test_objdump_uses_pc_not_the_accessed_address(self):
        with (
            mock.patch.object(tas_symbolize, "disassembly", return_value=" 8246de0:\tdivss 0x14(%ebp),%xmm0\n"),
            mock.patch.object(tas_symbolize, "resolve", return_value=(self.fn, 4)),
        ):
            self.assertEqual(
                tas_symbolize.instruction_of("/p", 0x8246DE0), f"0x8246de0 <{self.fn}+0x4>: divss 0x14(%ebp),%xmm0"
            )

    def test_bridge_records_registers_and_argument_slots_without_inferior_calls(self):
        bridge = Bridge(
            [], gdb_path="gdb", library_dir=None, breakpoint="stop", reason_expression="reason", run_dir=self.root
        )
        sent = []

        async def cli(command):
            sent.append(command)
            if bridge.stack_mode == "artifact":
                bridge.full_stack_lines.append(command)

        with mock.patch.object(bridge, "cli", side_effect=cli), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(bridge.stack("SIGFPE"))
        context = (self.root / "signal-context.txt").read_text()
        self.assertIn("info registers eip ebp esp mxcsr fctrl fstat", context)
        self.assertIn("x/16wx $ebp+8", context)
        self.assertFalse(any(command.startswith("call ") for command in sent))

    def test_tas_uses_configured_default_and_explicit_profile_wins(self):
        default, explicit = self.root / "tas.json", self.root / "other.json"
        for path in (default, explicit):
            path.write_text(json.dumps({"schema_version": 1, "name": path.stem}))
        for args, expected in ((["tas", "list"], default), (["--profile", str(explicit), "tas", "list"], explicit)):
            with (
                mock.patch.dict(os.environ, {"DECOMP_TAS_PROFILE": str(default)}),
                mock.patch.object(tas, "listing", side_effect=lambda p: {"profile": str(p.source_file)}),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(out.getvalue())["result"]["profile"], str(expected))
        missing = self.root / "missing-profile.json"
        with (
            mock.patch.dict(os.environ, {"DECOMP_TAS_PROFILE": str(missing)}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(main(["tas", "list"]), 1)
        self.assertIn(str(missing), out.getvalue())
