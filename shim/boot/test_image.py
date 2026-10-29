# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a resident data image from compiled fixture units and execute it."""
import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
parser = argparse.ArgumentParser()
parser.add_argument("--compiler", required=True)
parser.add_argument("--work", type=pathlib.Path, required=True)
parser.add_argument("--m32", action="store_true")
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("N64C_PORT_DIR", str(ROOT / "fixtures"))  # a port facts directory when set
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("boot_image", ROOT / "image.py")
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)
LAYOUT = """SECTIONS
{
    .span 0x80001000 : AT(0x1000) SUBALIGN(1)
    {
        src/A.CPP.o(.data);
        src/CODE.CPP.o(.text);
        src/ASM.s.o(.text);
        src/B.CPP.o(.data);
    }
    .ovl 0x80100000 : AT(0x2000) SUBALIGN(1)
    {
        src/O1.CPP.o(.data);
    }
    .ovl 0x80100000 : AT(0x3000) SUBALIGN(1)
    {
        src/O2.CPP.o(.data);
    }
}
"""
FACTS = """table = 0x80001000; // rom:0x1000
words = 0x80001010; // rom:0x1010
D_table_alias = 0x80001004; // rom:0x1004
grown = 0x80001020; // rom:0x1020
next = 0x80001024; // rom:0x1024
callback = 0x80200000; // type:func rom:0x5000
absent = 0x80200100; // type:func rom:0x5100
"""
UNITS = {
    "A.CPP": "unsigned int table[4] = {0x11223344, 2, 3, 4};\n"
             "extern void callback(void); extern void absent(void);\n"
             "void (*words[2])(void) = {callback, absent};\n",
    "B.CPP": "unsigned int grown[2] = {5, 6};\nunsigned char next[4] = {7};\n",
    "CODE.CPP": "int code(void) { return 1; }\n",
    "O1.CPP": "int o1 = 1;\n", "ASM.s": "nop\n", "O2.CPP": "int o2 = 2;\n",
}
LINKED = """extern unsigned int D_table_alias; extern void (*words[2])(void);
int calls; void callback(void) { calls++; }
int probe(void) { return (int)D_table_alias; }
"""
MAIN = r"""#include <stdio.h>
#include <string.h>
#include "boot.h"
extern int calls;
int main(void) {
    for (size_t i = 0; i < n64c_boot_image_count; i++) {
        const N64cBootImage *part = &n64c_boot_image[i];
        printf("%x %u %u\n", part->ram, part->offset, part->bytes);
    }
    unsigned int table[4], words[2];
    memcpy(table, n64c_boot_image_bytes, sizeof table);
    memcpy(words, n64c_boot_image_bytes + 16, sizeof words);
    ((void (*)(void))words[0])();
    printf("%x %u %d\n", table[0], table[3], calls);
    fflush(stdout);
    ((void (*)(void))words[1])();
    return 0;
}
"""
BOOT_H = """#include <stddef.h>
typedef unsigned int u32;
typedef struct { u32 ram, offset, bytes; } N64cBootImage;
typedef struct { u32 rom, offset, bytes; } N64cBootRomImage;
extern const N64cBootImage n64c_boot_image[];
extern const size_t n64c_boot_image_count;
extern const unsigned char n64c_boot_image_bytes[];
"""
# Per-version layout: the overlay section has one LMA per version; only the active version's lines count.
VERSIONED = """SECTIONS
{
#if defined(VERSION_OTHER)
    .ovl_80400000 0x80400000 : AT(0x5000) SUBALIGN(1)
#elif defined(VERSION_TEST)
    .ovl_80400000 0x80400000 : AT(0x6000) SUBALIGN(1)
#endif
    {
#if defined(VERSION_OTHER)
        src/OTHER.CPP.o(.data);
#endif
#if defined(VERSION_TEST) || defined(VERSION_THIRD)
        src/CODE.CPP.o(.text);
        src/SETUP.CPP.o(.data);
#endif
    }
}
"""
SETUP = """struct Info { int unused; };
typedef struct { struct Info *info; unsigned long flags; int behaviour; float gravity; } Setup;
extern struct Info target;
Setup setups[2] = {{&target, 0x3C5729, 5, -2304.0f}, {0}};
unsigned char misplaced[4] = {1, 2, 3, 4};
int zeroed;
unsigned int D_blob_0123abcd[1] = {7};
unsigned int func_80400400_de[1] = {0x27BDFFC8};
"""
SETUP_FACTS = """setups = 0x80400100; // rom:0x6100
misplaced = 0x80400120; // rom:0x9999
zeroed = 0x80400200; //
D_blob_0123abcd = 0x80400300; // rom:0x6300
func_80400400_de = 0x80400400; // rom:0x6400
target = 0x80001000; //
"""
# Raw code addresses: the source keeps numbers the cartridge shows to be code addresses (a KUSEG function alias
# beside a data pointer, a KSEG0 function start), a lone round value, and the same alias typed as bytes.
RAW = """unsigned int raw[4] = {0x00200010, 0x80001000, 0x80200020, 7};
unsigned int sizes[4] = {0x00200000, 1, 2, 3};
unsigned char packed[4] = {0x00, 0x20, 0x00, 0x10};
"""
RAW_FACTS = """raw = 0x80001100; // rom:0x1100
sizes = 0x80001200; // rom:0x1200
packed = 0x80001300; // rom:0x1300
func_80200000 = 0x80200000; // type:func
func_80200010 = 0x80200010; // type:func
func_80200020 = 0x80200020; // type:func
"""
MISSING = '#include <stdio.h>\n#include <stdlib.h>\nvoid n64c_boot_missing(const char *s) ' \
          '{ printf("boot-missing: %s\\n", s); exit(78); }\n'


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        self.source = self.path / "source"
        (self.source / "versions/test").mkdir(parents=True)
        (self.source / "src").mkdir()
        (self.source / "RageWars.ld").write_text(LAYOUT)
        (self.source / "versions/test/symbol_addrs.txt").write_text(FACTS)
        for unit, text in UNITS.items():
            (self.source / "src" / unit).write_text(text)
        self.manifest = self.path / "project.json"
        self.manifest.write_text(json.dumps({"version": "test", "rdram_bytes": "0x400000",
            "sources": ["src/CODE.CPP"], "data": {}, "bindings": {}, "stops": {}}))

    def compile(self, source, output, *extra):
        command = [args.compiler, "-c", "-fdata-sections", "-ffunction-sections", *extra, str(source), "-o", str(output)]
        if args.m32:
            command.insert(1, "-m32")
        subprocess.run(command, check=True, capture_output=True, text=True)
        return output

    @unittest.skipUnless(shutil.which("i686-w64-mingw32-gcc"), "needs the mingw-w64 cross compiler")
    def test_semantic_kuseg_record_classification(self):
        import addrscan
        targets = addrscan.Targets({"g_collisionSetupTableResource": (0x0044B4A0, "data")})
        self.assertEqual(targets.classify(0x0044B4A0), ("tlb-data", "g_collisionSetupTableResource"))

    def test_windows_object_reads_like_elf(self):
        """A Windows/i386 object gives the image the same view as an ELF one: stripped names, sizes, relocations."""
        source = self.path / "w.c"
        source.write_text("unsigned int table[4] = {1, 2, 3, 4};\nextern int ext;\nint *pp = &ext;\nint fn(void) { return 1; }\n")
        output = self.path / "w.o"
        subprocess.run(["i686-w64-mingw32-gcc", "-ffunction-sections", "-fdata-sections", "-c", str(source), "-o", str(output)],
                       check=True, capture_output=True, text=True)
        old = image.hostabi.PREFIX
        image.hostabi.PREFIX = "_"
        try:
            coff = image.load_object(output)
        finally:
            image.hostabi.PREFIX = old
        by_name = {symbol["name"]: symbol for symbol in coff.symbols if symbol["name"]}
        self.assertEqual((by_name["table"]["type"], by_name["table"]["size"]), (image.STT_OBJECT, 16))
        self.assertEqual((by_name["pp"]["type"], by_name["pp"]["size"]), (image.STT_OBJECT, 4))
        self.assertEqual(by_name["fn"]["type"], image.STT_FUNC)
        self.assertEqual((by_name["ext"]["shndx"], by_name["ext"]["bind"]), (0, 1))
        self.assertTrue(coff.data_section(by_name["table"]["shndx"]))
        self.assertFalse(coff.data_section(by_name["fn"]["shndx"]))
        relocations = [(where, coff.symbols[index]["name"], kind) for kind_list in coff.relocations.values()
                       for where, index, kind in kind_list]
        self.assertIn((0, "ext", image.R_386_32), relocations)

    def test_unit_that_does_not_compile_contributes_nothing(self):
        (self.source / "src" / "BAD.CPP").write_text("int broken = ;\n")
        output = self.path / "bad" / "BAD.CPP.o"
        image.compile_unit(ROOT / "prepare.py", args.compiler, self.source / "src" / "BAD.CPP", output,
                           (["-m32"] if args.m32 else []) + ["-std=gnu89"])
        self.assertEqual(output.read_bytes(), b"")
        self.assertTrue(pathlib.Path(f"{output}.err").read_text())
        image.build(self.manifest, self.source, [output], [], self.path / "out")
        report = json.loads((self.path / "out" / "image.json").read_text())
        self.assertEqual(report["failed_units"], [str(output)])

    def test_kuseg_alias_of_a_function_is_a_code_pointer(self):
        # D_00XXXXXX pinned at a function's address without its KSEG0 bit names the function (a word naming it must
        # bind to the native definition); an alias with no function there, or a high address, stays data
        (self.source / "versions/test/symbol_addrs.txt").write_text(
            "func_80239FCC_de = 0x80239FBC; // type:func\nD_00239FCC = 0x00239FBC; //\n"
            "rw_paged_func_80239FCC_de = 0x00239FBC; //\n"
            "D_00239FD0 = 0x00239FD0; //\nD_00900000 = 0x00900000; //\nfunc_80900000 = 0x80900000; // type:func\n")
        table = image.facts(self.source, "test")
        self.assertEqual(table["D_00239FCC"], (0x00239FBC, "func"))
        self.assertEqual(table["rw_paged_func_80239FCC_de"], (0x00239FBC, "func"))  # any name at a function's alias
        self.assertEqual(table["D_00239FD0"], (0x00239FD0, "data"))
        self.assertEqual(table["D_00900000"], (0x00900000, "data"))
        # a word naming the alias binds to the function itself (a stop on the function while nothing links it)
        (self.source / "src" / "ALIAS.CPP").write_text("extern char D_00239FCC[]; unsigned int table[1] = "
                                                       "{(unsigned int)D_00239FCC};\n")
        (self.source / "versions/test/symbol_addrs.txt").write_text(
            "func_80239FCC_de = 0x80239FBC; // type:func\nD_00239FCC = 0x00239FBC; //\n"
            "table = 0x80001000; // rom:0x1000\n")
        unit = self.compile(self.source / "src" / "ALIAS.CPP", self.path / "ALIAS.CPP.o", "-x", "c")
        image.build(self.manifest, self.source, [unit], [], self.path / "alias")
        report = json.loads((self.path / "alias" / "image.json").read_text())
        self.assertEqual(report["function_stops"], ["func_80239FCC_de"])
        self.assertIn(".long func_80239FCC_de", (self.path / "alias" / "image.c").read_text())

    def test_data_of_a_unit_that_also_holds_functions_is_imaged(self):
        # the decomp folds data units into code modules: such a unit's initialised data is imaged at its pinned
        # address even when the unit is linked as code, its functions are not
        (self.source / "RageWars.ld").write_text(LAYOUT.replace("src/B.CPP.o(.data);", "src/B.CPP.o(.data);\n        src/M.CPP.o(.data);"))
        (self.source / "src" / "M.CPP").write_text("int fold(void) { return 1; }\nunsigned int D_folded[1] = {0x3F800000};\n")
        (self.source / "versions/test/symbol_addrs.txt").write_text(FACTS + "D_folded = 0x80001030; // rom:0x1030\n")
        linked_code = {"src/CODE.CPP", "src/M.CPP"}
        self.assertIn("src/M.CPP", image.select(self.source, linked_code, "test"))
        self.assertNotIn("src/M.CPP", image.select(self.source, linked_code))
        self.assertNotIn("src/M.CPP", image.select(self.source, linked_code, "test", data_only=True))
        unit = self.compile(self.source / "src" / "M.CPP", self.path / "M.CPP.o", "-x", "c")
        image.build(self.manifest, self.source, [unit], [], self.path / "mixed")
        report = json.loads((self.path / "mixed" / "image.json").read_text())
        self.assertEqual(report["code_units"], [str(unit)])
        self.assertEqual(report["extents"], 1)
        self.assertNotIn("D_folded", report["refused"])
    def test_resident_data_of_a_unit_with_bss_in_an_overlay_is_imaged(self):
        # a unit whose code and zero-initialised sections sit in a shared-address overlay still has its resident data
        layout = LAYOUT.replace("src/B.CPP.o(.data);", "src/B.CPP.o(.data);\n        src/M.CPP.o(.data);")
        layout = layout.replace("src/O1.CPP.o(.data);", "src/O1.CPP.o(.data);\n        src/M.CPP.o(.text);\n        src/M.CPP.o(.bss);")
        (self.source / "RageWars.ld").write_text(layout)
        (self.source / "src" / "M.CPP").write_text("int fold(void) { return 1; }\nunsigned int D_folded[1] = {0x3F800000};\n")
        self.assertIn("src/M.CPP", image.select(self.source, {"src/CODE.CPP", "src/M.CPP"}, "test"))

    def test_select_keeps_resident_function_free_units(self):
        self.assertEqual(image.select(self.source, {"src/CODE.CPP"}), ["src/A.CPP", "src/B.CPP"])

    def test_overlay_data_is_keyed_by_rom_offset(self):
        # an overlay data unit of the project's version is imaged at LMA + (pinned address - VMA): the PI layer
        # writes those native bytes over the overlay's DMA; bss and a ROM offset that contradicts its fact are refused
        (self.source / "RageWars.ld").write_text(VERSIONED)
        (self.source / "versions/test/symbol_addrs.txt").write_text(SETUP_FACTS)
        (self.source / "src" / "SETUP.CPP").write_text(SETUP)
        (self.source / "src" / "OTHER.CPP").write_text("int other = 1;\n")
        self.assertEqual(image.overlay_units(self.source, "test"), {"src/SETUP.CPP": (0x80400000, 0x6000)})
        self.assertEqual(image.select(self.source, {"src/CODE.CPP"}, "test"), ["src/SETUP.CPP"])
        self.assertEqual(image.select(self.source, set(), "other"), ["src/OTHER.CPP"])
        (self.path / "src").mkdir()
        unit = self.compile(self.source / "src" / "SETUP.CPP", self.path / "src" / "SETUP.CPP.o", "-x", "c",
                            "-fno-common")
        out = self.path / "out"
        image.build(self.manifest, self.source, [unit], [], out)
        report = json.loads((out / "image.json").read_text())
        self.assertEqual(report["overlay_symbols"], ["setups"])
        self.assertEqual(report["rom_extents"], [["0x6100", 32]])
        self.assertEqual(report["extents"], 0)
        self.assertEqual(report["refused"]["zeroed"], "overlay bss has no ROM bytes")
        self.assertEqual(report["refused"]["misplaced"], "overlay ROM offset differs from its rom fact")
        self.assertIn("placeholder", report["refused"]["D_blob_0123abcd"])
        self.assertIn("placeholder", report["refused"]["func_80400400_de"])
        self.assertIn("{0x6100u, 0u, 32u},", (out / "image.c").read_text())
        rows = (out / "image.bin").read_bytes()
        self.assertEqual(rows[:16], bytes.fromhex("00100080 29573c00 05000000 000010c5"))
        self.assertEqual(rows[16:], bytes(16))

    def cartridge(self, words):
        rom = bytearray(0x2000)
        for at, values in words.items():
            for i, value in enumerate(values):
                rom[at + 4 * i:at + 4 * i + 4] = value.to_bytes(4, "big")
        return bytes(rom)

    def test_raw_code_addresses_bind_to_their_functions(self):
        # with the cartridge, a native word equal to the ROM word that names a function (KSEG0, or a KUSEG alias with
        # a pointer beside it) is bound like a relocation (a generated stop here: nothing links the function); a lone
        # KUSEG value and bytes the source types otherwise stay as they are; without the cartridge nothing changes
        (self.source / "versions/test/symbol_addrs.txt").write_text(RAW_FACTS)
        (self.source / "src" / "RAW.CPP").write_text(RAW)
        unit = self.compile(self.source / "src" / "RAW.CPP", self.path / "RAW.CPP.o", "-x", "c")
        rom = self.cartridge({0x1100: [0x00200010, 0x80001000, 0x80200020, 7], 0x1200: [0x00200000, 1, 2, 3],
                              0x1300: [0x00200010]})
        out = self.path / "out"
        image.build(self.manifest, self.source, [unit], [], out, rom)
        report = json.loads((out / "image.json").read_text())
        self.assertTrue(report["rom_checked"])
        self.assertEqual([(row["address"], row["word"], row["function"]) for row in report["code_words"]],
                         [("0x80001100", "0x00200010", "func_80200010"), ("0x80001108", "0x80200020", "func_80200020")])
        self.assertEqual(report["function_stops"], ["func_80200010", "func_80200020"])
        self.assertIn(".long func_80200010", (out / "image.c").read_text())
        image.build(self.manifest, self.source, [unit], [], self.path / "plain")
        report = json.loads((self.path / "plain" / "image.json").read_text())
        self.assertEqual((report["rom_checked"], report["code_words"], report["function_stops"]), (False, [], []))

    def test_typed_resident_data_covers_its_rom_dmas(self):
        # a typed resident symbol's ROM bytes are also an image range: a game DMA of them (a TLB window over the
        # cartridge) sees its native bytes; an untyped emitter placeholder keeps the cartridge's bytes there
        (self.source / "versions/test/symbol_addrs.txt").write_text(
            "typed = 0x80001100; // rom:0x1100\nD_blob_00aa = 0x80001200; // rom:0x1200\n")
        (self.source / "src" / "TYPED.CPP").write_text(
            "unsigned short typed[4] = {1, 2, 3, 4};\nunsigned int D_blob_00aa[2] = {5, 6};\n")
        unit = self.compile(self.source / "src" / "TYPED.CPP", self.path / "TYPED.CPP.o", "-x", "c")
        out = self.path / "out"
        image.build(self.manifest, self.source, [unit], [], out, self.cartridge({}))
        report = json.loads((out / "image.json").read_text())
        self.assertEqual(report["rom_extents"], [["0x1100", 8]])
        self.assertEqual(report["extents"], 2)
        self.assertIn("{0x1100u, 0u, 8u},", (out / "image.c").read_text())
    def test_address_scan_lists_code_words_and_guards_commands(self):
        # addrscan.code_word: a display-list command argument is never a code word; a KUSEG alias needs a pointer
        # within 6 words; a KUSEG data record (D_00XXXXXX) is tlb-data
        sys.path.insert(0, str(ROOT))
        import addrscan
        table = {"func_80200000": (0x80200000, "func"), "func_80200010": (0x80200010, "func"),
                 "D_0044B4A0": (0x0044B4A0, "data")}
        targets = addrscan.Targets(table)
        rom = self.cartridge({0x100: [0x00200010, 0x80001000], 0x200: [0xDE000000, 0x80200010],
                              0x300: [0x80200010], 0x400: [0x00200000], 0x500: [0x00200010]})
        self.assertEqual(addrscan.code_word(rom, 0x100, targets), "func_80200010")
        self.assertIsNone(addrscan.code_word(rom, 0x204, targets))
        self.assertEqual(addrscan.code_word(rom, 0x300, targets), "func_80200010")
        self.assertIsNone(addrscan.code_word(rom, 0x400, targets))  # a lone page-aligned value stays a size
        self.assertEqual(addrscan.code_word(rom, 0x500, targets), "func_80200010")  # a lone off-page start is code
        self.assertIsNone(
                          addrscan.code_class(rom, 0x500, targets, lone=False))  # unless in raw binary
        self.assertEqual(targets.classify(0x0044B4A0), ("tlb-data", "D_0044B4A0"))
        self.assertIsNone(targets.classify(0x00300000))

    def test_entry_alias_binds_over_register_only_prologue_skips(self):
        # a KUSEG alias k instructions into a function binds to it (class "entry") when every skipped instruction is
        # a register-only ALU op; a load, branch or call among them, or too deep an entry, leaves the word unbound
        sys.path.insert(0, str(ROOT))
        import addrscan
        sll, addiu, lw, jal = 0x00051080, 0x27BDFFC0, 0x8FA20010, 0x0C000000
        table = {"func_80200010": (0x80200010, "func")}
        targets = addrscan.Targets(table, roms={"func_80200010": 0x1000})
        rom = bytearray(0x2000)
        for at, word in ((0x1000, sll), (0x1004, addiu), (0x1008, lw), (0x100C, sll), (0x1010, sll), (0x1014, sll)):
            rom[at:at + 4] = word.to_bytes(4, "big")
        for at, word in ((0x100, 0x00200014), (0x104, 0x00200018), (0x108, 0x0020001C), (0x10C, 0x00200024),
                         (0x110, 0x00200010)):
            rom[at:at + 4] = word.to_bytes(4, "big")
        self.assertEqual(addrscan.code_class(rom, 0x100, targets), ("func_80200010", "entry"))
        self.assertEqual(addrscan.code_class(rom, 0x104, targets), ("func_80200010", "entry"))  # sll, addiu
        self.assertIsNone(addrscan.code_class(rom, 0x108, targets))  # skips a load
        self.assertIsNone(addrscan.code_class(rom, 0x10C, targets))  # skips more than MAX_SKIP instructions
        self.assertEqual(addrscan.code_class(rom, 0x110, targets), ("func_80200010", "exact"))
        self.assertFalse(addrscan.register_alu(jal) or addrscan.register_alu(lw))
        self.assertTrue(addrscan.register_alu(sll) and addrscan.register_alu(addiu))
        self.assertIsNone(addrscan.code_class(rom, 0x100, addrscan.Targets(table)))  # no ROM fact, no entry

    def test_entry_alias_in_a_typed_blob_is_bound_by_the_image(self):
        (self.source / "versions/test/symbol_addrs.txt").write_text(
            "D_blob_0123abcd = 0x80001100; // rom:0x1100\nfunc_80200010 = 0x80200010; // type:func rom:0x1500\n")
        (self.source / "src" / "RAW.CPP").write_text("unsigned int D_blob_0123abcd[3] = {0xE06, 0x13, 0x00200014};\n")
        unit = self.compile(self.source / "src" / "RAW.CPP", self.path / "RAW.CPP.o", "-x", "c")
        rom = bytearray(self.cartridge({0x1100: [0xE06, 0x13, 0x00200014]}))
        rom[0x1500:0x1504] = (0x00051080).to_bytes(4, "big")
        out = self.path / "out"
        image.build(self.manifest, self.source, [unit], [], out, bytes(rom))
        report = json.loads((out / "image.json").read_text())
        self.assertEqual([(row["word"], row["function"], row["class"]) for row in report["code_words"]],
                         [("0x00200014", "func_80200010", "entry")])

    def test_named_alias_into_a_function_binds_to_its_entry(self):
        # a type:func D_00XXXXXX name at a function's second instruction (a named reference, not a raw word) binds to
        # that function when the skipped instruction is register-only; without the cartridge it stays a stop on the name
        (self.source / "versions/test/symbol_addrs.txt").write_text(
            "func_80200010 = 0x80200010; // type:func rom:0x1500\nD_00200014 = 0x00200014; // type:func\n"
            "table = 0x80001100; // rom:0x1100\n")
        (self.source / "src" / "NAMED.CPP").write_text("extern char D_00200014[]; unsigned int table[1] = "
                                                       "{(unsigned int)D_00200014};\n")
        unit = self.compile(self.source / "src" / "NAMED.CPP", self.path / "NAMED.CPP.o", "-x", "c")
        rom = bytearray(self.cartridge({0x1100: [0]}))
        rom[0x1500:0x1504] = (0x00051080).to_bytes(4, "big")
        out = self.path / "named"
        image.build(self.manifest, self.source, [unit], [], out, bytes(rom))
        report = json.loads((out / "image.json").read_text())
        self.assertEqual(report["function_stops"], ["func_80200010"])
        self.assertIn(".long func_80200010", (out / "image.c").read_text())

    def test_layout_condition_refuses_unknown_forms(self):
        (self.source / "RageWars.ld").write_text("#if VERSION_TEST > 1\n#endif\n")
        with self.assertRaises(ValueError):
            image.placements(self.source, "test")

    def test_image_places_relocates_refuses_and_binds(self):
        objects = [self.compile(self.source / "src" / unit, self.path / (unit + ".o"), "-x", "c")
                   for unit in ("A.CPP", "B.CPP")]
        (self.path / "linked.c").write_text(LINKED)
        linked = self.compile(self.path / "linked.c", self.path / "linked.o")
        out = self.path / "out"
        image.build(self.manifest, self.source, objects, [linked], out)
        report = json.loads((out / "image.json").read_text())
        self.assertEqual(report["function_stops"], ["absent"])
        self.assertEqual(report["bound"], ["D_table_alias"])
        self.assertIn("overlaps next", report["refused"]["grown"])
        self.assertEqual(report["relocated_words"], 2)
        self.assertEqual(report["weak_stops"], [])
        self.assertIn("D_table_alias = 0x80001004;", (out / "image.ld").read_text())
        (self.path / "boot.h").write_text(BOOT_H)
        (self.path / "missing.c").write_text(MISSING)
        (self.path / "main.c").write_text(MAIN)
        executable = self.path / "probe"
        command = [args.compiler, "-no-pie", "-I" + str(self.path), str(out / "image.c"), str(linked),
                   str(self.path / "missing.c"), str(self.path / "main.c"), "-Wl,-T," + str(out / "image.ld"), "-o", str(executable)]
        if args.m32:
            command.insert(1, "-m32")
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout.splitlines(),
                         ["1000 0 24", "11223344 4 1", "boot-missing: absent"])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
