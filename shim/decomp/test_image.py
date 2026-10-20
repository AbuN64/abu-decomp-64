# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a resident data image from compiled fixture units and execute it."""
import argparse
import importlib.util
import json
import pathlib
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
extern const N64cBootImage n64c_boot_image[];
extern const size_t n64c_boot_image_count;
extern const unsigned char n64c_boot_image_bytes[];
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
            "D_00239FD0 = 0x00239FD0; //\nD_00900000 = 0x00900000; //\nfunc_80900000 = 0x80900000; // type:func\n")
        table = image.facts(self.source, "test")
        self.assertEqual(table["D_00239FCC"], (0x00239FBC, "func"))
        self.assertEqual(table["D_00239FD0"], (0x00239FD0, "data"))
        self.assertEqual(table["D_00900000"], (0x00900000, "data"))

    def test_select_keeps_resident_function_free_units(self):
        self.assertEqual(image.select(self.source, {"src/CODE.CPP"}), ["src/A.CPP", "src/B.CPP"])

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
