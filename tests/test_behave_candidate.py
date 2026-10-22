import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abu_decomp.behave import candidate as cand
from abu_decomp.behave import describe, inputs
from abu_decomp.config import load_profile
from abu_decomp.tools import DependencyError, api_call

SYMBOLS = {
    "func_a": [0x80001000, 0, 0x40, "func"],
    "func_b": [0x80002000, 0, 0x20, "func"],
    "D_x": [0x80100000, None, 4, "d"],
}

OBJDUMP_T = """
candidate.o:     file format elf32-tradbigmips

SYMBOL TABLE:
80800000 l    d  .text\t00000000 .text
80800000 l       .text\t00000000 gcc2_compiled.
80800000 g     F .text\t00000040 func_a
80800040 g     F .text\t00000000 func_b
80800060 l     F .text\t00000020 helper
00000000 g       *UND*\t00000000 D_x
"""


class LinkTests(unittest.TestCase):
    def test_script_pins_siblings_before_providing_every_symbol(self):
        text = cand.linker_script(0x80800000, SYMBOLS, {"func_b": 0x80002000})
        self.assertLess(text.index("func_b = 0x80002000;"), text.index("PROVIDE(func_a = 0x80001000);"))
        self.assertIn(". = 0x80800000;", text)
        self.assertIn("*(.rdata)", text)  # the assembler's name for read-only data

    def test_auto_data_labels_take_their_address_from_the_name(self):
        got = cand.named_addresses(["D_auto_800D7068", "D_auto_1B2FC", "D_x", "func_80290A70_us_rev1"])
        self.assertEqual(got, {"D_auto_800D7068": 0x800D7068})

    def test_a_symbol_without_an_address_is_not_provided(self):
        text = cand.linker_script(0x80800000, {"Z": [None, None, 4, "d"]})
        self.assertNotIn("PROVIDE(Z", text)

    def test_only_function_symbols_of_text_become_functions(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "objdump"
            tool.write_text(f"#!/bin/sh\ncat <<'EOF'\n{OBJDUMP_T}EOF\n")
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
            elf = Path(directory) / "x.elf"
            table = cand.function_table(str(tool), elf, 0x80800080, 0x80800000)
            # func_b has no size: it runs to the next symbol
            self.assertEqual(
                [(r["name"], r["offset"], r["size"]) for r in table],
                [("func_a", 0, 0x40), ("func_b", 0x40, 0x20), ("helper", 0x60, 0x20)],
            )


class BuildTests(unittest.TestCase):
    def build_with(self, relocs=""):
        """build() with fake binutils; `relocs` is what `objdump -r` prints. Returns the image, the link script and
        the build_context call."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ld").write_text('#!/bin/sh\necho linked > "$4"\n')
            (root / "objcopy").write_text("#!/bin/sh\nfor a; do last=$a; done\nprintf 'IMAGEBYTES' > \"$last\"\n")
            (root / "objdump").write_text(
                "#!/bin/sh\ncase \"$1\" in -h) printf '  0 .text 00000040 80800000 80800000 00010000 2**4\\n';;"
                f" -t) cat <<'EOF'\n{OBJDUMP_T}EOF\n;; -r) cat <<'EOF'\n{relocs}EOF\n;; esac\n"
            )
            for name in ("ld", "objcopy", "objdump"):
                (root / name).chmod(0o755)
            host = root / "host.toml"
            host.write_text(
                f'[tools]\nmips_ld = "{root}/ld"\nmips_objcopy = "{root}/objcopy"\nmips_objdump = "{root}/objdump"\n'
            )
            project = root / "project"
            project.mkdir()
            seat = root / "seat.json"
            seat.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "t",
                        "tools": {"project": str(project), "host": str(host), "timeout_seconds": 5},
                    }
                )
            )
            source = root / "f.c"
            source.write_text("int func_a(void) { return 0; }\n")
            context = {"source": "int func_a(void) { return 0; }\n", "script": 'printf OBJ > "$3"\n'}
            with (
                mock.patch.object(cand, "api_call", return_value=context) as api,
                mock.patch.object(cand, "symbol_table", return_value=SYMBOLS),
            ):
                image = cand.build(
                    load_profile(seat),
                    version="v1",
                    function="func_a",
                    source=source,
                    work=root / "work",
                    base=0x80800000,
                    project=None,
                    host=None,
                )
            self.assertEqual(image.path.read_bytes(), b"IMAGEBYTES")
            return image, (root / "work" / "link.ld").read_text(), api.call_args

    def test_build_compiles_links_and_returns_the_function_table(self):
        image, script, call = self.build_with()
        name, version, text = call.args[2]
        self.assertEqual((call.args[1], name, version), ("build_context", "func_a", "v1"))
        self.assertTrue(text.startswith("#define NON_MATCHING 1\n"))
        self.assertIn("func_b = 0x80002000;", script)  # a sibling with an original address stays original
        self.assertEqual(image.kept, ())
        self.assertEqual(image.spec("func_a")["entry"], "func_a")
        self.assertEqual([f["name"] for f in image.functions], ["func_a", "func_b", "helper"])

    def test_a_sibling_branched_to_pc_relative_stays_in_the_image(self):
        # `b func_b` (R_MIPS_PC16) reaches only +-128 KiB: pinning func_b at 0x80002000 would not link at 0x80800000
        relocs = (
            "RELOCATION RECORDS FOR [.text]:\nOFFSET   TYPE              VALUE\n"
            "00000010 R_MIPS_26         func_b\n00000030 R_MIPS_PC16       func_b\n00000034 R_MIPS_HI16       D_x+0x4\n"
        )
        image, script, _ = self.build_with(relocs)
        self.assertNotIn("func_b = 0x80002000;", script)
        self.assertIn("PROVIDE(func_b = 0x80002000);", script)  # the object's definition wins over PROVIDE
        self.assertEqual(image.kept, ("func_b",))

    def test_relocations_parse_offset_type_and_bare_symbol(self):
        out = "x.o: format\n\nRELOCATION RECORDS FOR [.text]:\nOFFSET   TYPE              VALUE\n" + (
            "000001f4 R_MIPS_PC16       func_802AD69C_de\n00000034 R_MIPS_LO16       D_x+0x00000010\n"
            "0000010c R_MIPS_26         .text\n"
        )
        with mock.patch.object(cand.subprocess, "run", return_value=mock.Mock(stdout=out)):
            relocs = cand.relocations("objdump", Path("x.o"))
        self.assertEqual(
            relocs,
            [(0x1F4, "R_MIPS_PC16", "func_802AD69C_de"), (0x34, "R_MIPS_LO16", "D_x"), (0x10C, "R_MIPS_26", ".text")],
        )
        self.assertEqual(cand.pc_targets(relocs), {"func_802AD69C_de"})

    def test_a_missing_tool_row_is_a_named_error(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "host.toml"
            host.write_text('[tools]\nmips_ld = "ld"\n')
            with self.assertRaises(cand.CandidateError) as caught:
                cand.host_tools(host)
            self.assertEqual(caught.exception.key, "behave.host")


class SiteTests(unittest.TestCase):
    def test_diagnostics_naming_the_preprocessed_unit_name_the_real_file_and_line_instead(self):
        lines = [["a.CPP", 1], ["include/X.H", 40], ["a.CPP", 3]]
        message = (
            "/w/candidate.o.i:3: conflicting types for `f' | /w/candidate.o.i:2: previous declaration of `f' | "
            "/w/candidate.o.i:99: odd"
        )
        self.assertEqual(
            cand.real_sites(message, lines),
            "a.CPP:3: conflicting types for `f' | include/X.H:40: previous declaration of `f' "
            "| /w/candidate.o.i:99: odd",
        )


class ErrorLineTests(unittest.TestCase):
    def test_errors_come_before_the_warnings_that_end_a_failed_compile(self):
        text = (
            "/w/c.i: In function `f':\n/w/c.i:3: warning: passing arg 1 of `g' from incompatible pointer type\n"
            "/w/c.i: At top level:\n/w/c.i:9: conflicting types for `h'\n/w/c.i:2: previous declaration of `h'\n"
            "/w/c.i:12: warning: array `d' assumed to have one element\n"
        )
        self.assertEqual(
            cand.error_lines(text), ["/w/c.i:9: conflicting types for `h'", "/w/c.i:2: previous declaration of `h'"]
        )

    def test_a_log_of_only_warnings_shows_its_last_lines(self):
        text = "".join(f"c.i:{n}: warning: w{n}\n" for n in range(1, 6))
        self.assertEqual(cand.error_lines(text), ["c.i:3: warning: w3", "c.i:4: warning: w4", "c.i:5: warning: w5"])


class DescribeTests(unittest.TestCase):
    def case(self, status, detail="", memory=None):
        return {
            "name": "c",
            "status": status,
            "detail": detail,
            "events": [],
            "reads": [],
            "unseeded": [],
            "inputs": {"memory": memory or []},
        }

    def test_a_fault_at_a_tag_names_the_word_that_held_it(self):
        learned = inputs.Learned(hot=((1, ((0x24, 4),)),), globals=(0x80101000, 0x80102000), stubs=("get",))
        tags = inputs.Tags.of(learned)
        tag = tags.value("field", (1, 0x24)) + 0x10
        faults = [self.case("unmapped", f"unmapped {tag:08X} 4 last 0"), self.case("ok")]
        self.assertEqual(describe.pointer_fields(faults, tags, inputs.Learned()), ([(1, 0x24)], []))
        known = inputs.Learned(pointers=((1, 0x24),))
        self.assertEqual(describe.pointer_fields(faults, tags, known), ([], []))  # already known
        g = [self.case("mmio", f"mmio {tags.value('global', 0x80102000):08X} 4 last 0")]
        self.assertEqual(describe.pointer_fields(g, tags, inputs.Learned()), ([(-1, 0x80102000)], []))
        s = [self.case("unmapped", f"unmapped {tags.value('stub', 'get') + 0x14:08X} 4 last 0")]
        self.assertEqual(describe.pointer_fields(s, tags, inputs.Learned()), ([], ["get"]))
        self.assertEqual(describe.pointer_fields(s, tags, inputs.Learned(returns=("get",))), ([], []))

    def test_a_displacement_of_a_whole_struct_still_names_the_word_that_held_the_tag(self):
        tags = inputs.Tags.of(inputs.Learned(hot=((0, ((0xB0, 4), (0x104, 4))),)))
        for field in (0xB0, 0x104):
            for displacement in (0x2B98, -0x8000, 0x7FFF, 0):
                at = tags.value("field", (0, field)) + displacement
                self.assertEqual(tags.decode(at), ("field", (0, field)))

    def test_globals_split_into_state_and_initialised_data(self):
        case = self.case("ok")
        case["reads"] = [[0x80100000, 4], [0x803FFF00, 4]]
        case["unseeded"] = [[0x80200000, 4]]
        seen, state = describe.read_globals([case], [], (0x803F0000, 0x80400000))
        self.assertEqual((seen, state), ((0x80100000, 0x80200000), (0x80200000,)))


class ApiTests(unittest.TestCase):
    def test_only_the_listed_api_calls_are_allowed(self):
        with self.assertRaises(DependencyError):
            api_call(mock.Mock(), "load", [])


if __name__ == "__main__":
    unittest.main()
