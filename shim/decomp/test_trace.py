# SPDX-License-Identifier: GPL-3.0-or-later
"""Build and run an instrumented fixture through the boot hit census."""
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
spec = importlib.util.spec_from_file_location("boot_trace", ROOT / "trace.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
GAME = r'''
extern void n64c_trace_event(const char *) __attribute__((weak));
void alpha(void) {}
void beta(void) { alpha(); }
void gamma_fn(void) {}
void delta(void) {}
void shimmed(void) {}
int real_main(void) {
    n64c_trace_event("first thread start");
    n64c_trace_event("first thread start");
    beta();  /* beta, then alpha: the oracle saw alpha first */
    beta();
    delta(); /* gamma_fn (order 3) is never reached before delta (order 4) */
    return 0;
}
'''
TRACE = "order\taddress\tsymbol\tdecomp_name\n1\t0x80000010\talpha\n2\t0x80000020\tbeta_old\tbeta\n" \
        "3\t0x80000030\tgamma_fn\n4\t0x80000040\tdelta\n5\t0x80000050\tshimmed\n6\t0x80000060\tnot_linked\n"
class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        facts = self.path / "source/versions/test"
        facts.mkdir(parents=True)
        (facts / "symbol_addrs.txt").write_text("alpha = 0x80000010; //\nbeta = 0x80000020; //\n")
        (self.path / "trace.tsv").write_text(TRACE)
        project = {"version": "test", "sources": [], "bindings": {"shimmed": "host_shim"}, "stops": {}}
        (self.path / "input.json").write_text(json.dumps(project))
    def generate(self):
        module.generate(self.path / "trace.tsv", self.path / "input.json", self.path / "source", self.path / "out")
        return (self.path / "out/trace_table.c").read_text()
    def test_table_names_and_shim_rows(self):
        table = self.generate()
        self.assertIn('{2, 0x80000020u, 0, {"beta_old", "beta"}}', table)
        self.assertIn('{5, 0x80000050u, 1, {"shimmed"}}', table)
        self.assertIn("n64c_trace_count = 6;", table)
    def test_bad_trace_is_refused(self):
        for text in ["order\taddress\tsymbol\n2\t0x80000010\talpha\n", "order\taddress\tsymbol\n1\t0x80000010\n",
                     "order\taddress\tsymbol\n1\t0x80000010\tbad name\n", "order\taddress\tsymbol\n"]:
            (self.path / "trace.tsv").write_text(text)
            with self.assertRaises(ValueError):
                self.generate()
    def test_instrumented_run_prints_first_hits(self):
        self.generate()
        (self.path / "game.c").write_text(GAME)
        flags = ["-m32"] if args.m32 else []
        objects = []
        for source, extra in [(self.path / "game.c", ["-finstrument-functions"]),
                              (ROOT / "trace.c", []), (self.path / "out/trace_table.c", [])]:
            obj = self.path / (source.stem + ".o")
            subprocess.run([args.compiler, *flags, *extra, "-I" + str(ROOT), "-c", str(source), "-o", str(obj)],
                           check=True, capture_output=True, text=True)
            objects.append(str(obj))
        main_obj = self.path / "driver.o"
        driver = self.path / "driver.c"
        driver.write_text("extern void n64c_trace_init(void);\nextern void n64c_trace_summary(void);\n"
                          "extern int real_main(void);\nint main(void){n64c_trace_init();int r=real_main();"
                          "n64c_trace_summary();return r;}\n")
        subprocess.run([args.compiler, *flags, "-I" + str(ROOT), "-c", str(driver), "-o", str(main_obj)],
                       check=True, capture_output=True, text=True)
        exe = self.path / "check"
        subprocess.run([args.compiler, *flags, "-no-pie", str(main_obj), *objects,
                        "-ldl", "-lpthread", "-o", str(exe)], check=True, capture_output=True, text=True)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.splitlines(), [
            "boot-step: trace armed: 4 of 6 functions watched (1 shim-bound, 1 not linked)",
            "boot-step: first thread start (after 0/6)",
            "boot-step: 1/6 beta",
            "boot-step: out of oracle order: beta (order 2) before alpha (order 1)",
            "boot-step: 2/6 alpha",
            "boot-step: 3/6 delta",
            "boot-step: out of oracle order: delta (order 4) before gamma_fn (order 3)",
            "boot-step: census 3/6 hit, 4 watched, last delta (order 4), next in oracle order gamma_fn (order 3)",
        ])
unittest.main(argv=[sys.argv[0]])
