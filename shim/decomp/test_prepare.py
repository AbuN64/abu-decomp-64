# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise real preprocessing, compilation, ABI calls and terminal stops."""
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
spec = importlib.util.spec_from_file_location("prepare", ROOT / "prepare.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        self.flags = ["-std=gnu89"]
        if args.m32:
            self.flags.append("-m32")

    def generate(self, code):
        src = self.path / "source.c"
        src.write_text(code)
        files = [self.path / n for n in ("source.i", "stubs.c", "inventory.json", "source.d")]
        status = module.prepare(args.compiler, src, *files,
            self.flags + ["-DNON_MATCHING", "-DVERSION_US_REV1",
                          "-include", str(ROOT / "native.h")])
        self.assertEqual(status, 0)
        return files, json.loads(files[2].read_text())

    def executable(self, files, main):
        driver = self.path / "main.c"
        driver.write_text(main)
        exe = self.path / "check"
        subprocess.run([args.compiler, *self.flags, "-I" + str(ROOT.parent), str(files[0]), str(files[1]),
                        str(ROOT / "missing.c"), str(driver), "-o", str(exe)],
                       check=True, capture_output=True, text=True)
        return exe

    def test_uncompilable_nonmatching_falls_back_to_matched_c(self):
        files, data = self.generate("""
extern int shared;
int matched(void) { return 7; }
#ifdef NON_MATCHING
extern char shared;
int fuzzy(void) { return shared; }
#else
INCLUDE_ASM("text", fuzzy);
#endif
""")
        self.assertEqual(data["mode"], "matched")
        self.assertEqual(data["missing"], ["fuzzy"])
        self.assertTrue(any("conflicting types" in e for e in data["non_matching_errors"]))
        exe = self.executable(files, "int matched(void); void fuzzy(void);\n"
                              "int main(int c, char **v) { (void)v; if (c > 1) fuzzy(); return matched() - 7; }\n")
        self.assertEqual(subprocess.run([exe]).returncode, 0)
        stop = subprocess.run([exe, "x"], capture_output=True, text=True)
        self.assertEqual(stop.returncode, 78)
        self.assertIn("boot-missing: fuzzy", stop.stderr)
    def test_active_branches_and_nonmatching_body(self):
        files, data = self.generate("""
const char quote = '"';
#if defined(VERSION_US_REV1)
extern long absent(int, double);
INCLUDE_ASM("text", absent);
#else
INCLUDE_ASM("text", wrong_version);
#endif
#ifdef NON_MATCHING
int actual(void) { return 23; }
#else
INCLUDE_ASM("text", actual);
#endif
const char *literal = "extern void n64c_missing_marker_fake(void);";
""")
        self.assertEqual(data["missing"], ["absent"])
        self.assertIn("native.h", files[3].read_text())
        exe = self.executable(files, """
extern int actual(void);
extern long absent(int, double);
int main(int argc, char **argv) {
    if (actual() != 23) return 91;
    if (argc > 1) absent(7, 2.5);
    return 0;
}
""")
        self.assertEqual(subprocess.run([exe]).returncode, 0)
        stopped = subprocess.run([exe, "stop"], capture_output=True, text=True)
        self.assertEqual((stopped.returncode, stopped.stderr), (78, "boot-missing: absent\n"))

    def test_data_remains_unresolved(self):
        files, data = self.generate("""
INCLUDE_DATA("data", table);
INCLUDE_RODATA("rodata", names);
INCLUDE_BSS("bss", state, 64);
extern int table[];
int read_table(void) { return table[0]; }
""")
        self.assertEqual(data["data"], ["names", "state", "table"])
        self.assertEqual(data["missing"], [])
        driver = self.path / "main.c"
        driver.write_text("extern int read_table(void); int main(void) { return read_table(); }")
        result = subprocess.run([args.compiler, *self.flags, str(files[0]), str(files[1]),
            str(driver), "-o", str(self.path / "invalid")], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("table", result.stderr)

    def test_preprocessing_error_is_not_a_stub(self):
        src = self.path / "invalid.c"
        src.write_text('#error broken_source\nINCLUDE_ASM("text", wrong);\n')
        files = [self.path / n for n in ("source.i", "stubs.c", "inventory.json", "source.d")]
        status = module.prepare(args.compiler, src, *files, self.flags)
        self.assertNotEqual(status, 0)
        self.assertFalse(files[0].exists())
        self.assertFalse(files[1].exists())

    @unittest.skipUnless(args.m32, "source va_list ABI requires m32")
    def test_native_varargs(self):
        files, data = self.generate("""
typedef char *va_list;
#include "stdarg.h"
double sum(int count, ...) {
    va_list ap;
    double value;
    va_start(ap, count);
    value = va_arg(ap, double);
    value += va_arg(ap, int);
    va_end(ap);
    return value;
}
""")
        exe = self.executable(files, """
extern double sum(int count, ...);
int main(void) { return sum(2, 3.25, 7) != 10.25; }
""")
        self.assertEqual(subprocess.run([exe]).returncode, 0)


unittest.main(argv=["test_prepare"], verbosity=2)
