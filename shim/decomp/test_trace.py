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
    @unittest.skipUnless(args.m32, "lockstep return trampolines on i386 only")
    def test_lockstep_guest_shutdown_returns_through_host_entry(self):
        self.generate()
        (self.path / "game.c").write_text(r"""
#include "n64c_os.h"
#include "n64c_internal.h"
static OSMesgQueue queue;
static OSMesg slot[1];
static OSThread threads[3];
static int returned;
__attribute__((noinline)) void alpha(void) {
    OSMesg message;
    osRecvMesg(&queue, &message, OS_MESG_BLOCK);
    returned++;
}
__attribute__((noinline)) void beta(void) {
    osDestroyThread(NULL);
    returned++;
}
static void blocked(void *unused) { (void)unused; alpha(); }
static void destroyed(void *unused) { (void)unused; beta(); }
int real_main(void) {
    n64c_sched_reset();
    osCreateMesgQueue(&queue, slot, 1);
    osCreateThread(&threads[0], 1, blocked, NULL, NULL, 10);
    osCreateThread(&threads[1], 2, blocked, NULL, NULL, 20);
    osStartThread(&threads[0]); osStartThread(&threads[1]);
    n64c_sched_run_idle();
    if (n64c_sched_waiting() != 2) return 1;
    n64c_sched_reset(); /* both live alpha frames have lockstep return hooks */
    if (returned || n64c_sched_alive()) return 2;
    osCreateThread(&threads[2], 3, destroyed, NULL, NULL, 10);
    osStartThread(&threads[2]);
    n64c_sched_run_idle();
    n64c_sched_reset(); /* beta destroyed itself through the same host boundary */
    return returned || n64c_sched_alive();
}
""")
        (self.path / "driver.c").write_text(r"""
#include "trace.h"
#include <stdint.h>
static unsigned char ram[0x400000];
void *n64c_memory_ram(unsigned physical, unsigned bytes) {
    return physical <= sizeof ram && bytes <= sizeof ram - physical ? ram + physical : 0;
}
unsigned n64c_memory_physical(const void *p) { return (unsigned)(uintptr_t)p; }
void n64c_fpu_thread_start(void) {}
void n64c_vi_interrupt(void) {}
extern int real_main(void);
int main(void) { n64c_trace_init(); return real_main(); }
""")
        objects = []
        for source, extra in [(self.path / "game.c", ["-finstrument-functions", "-fno-omit-frame-pointer"]),
                              (self.path / "driver.c", []), (ROOT / "trace.c", []),
                              (ROOT.parent / "n64c_sched.c", []), (self.path / "out/trace_table.c", [])]:
            obj = self.path / (source.stem + ".o")
            subprocess.run([args.compiler, "-m32", "-O1", *extra, "-I" + str(ROOT), "-I" + str(ROOT.parent),
                            "-c", str(source), "-o", str(obj)], check=True, capture_output=True, text=True)
            objects.append(str(obj))
        exe = self.path / "shutdown"
        subprocess.run([args.compiler, "-m32", "-no-pie", *objects, "-ldl", "-lpthread", "-o", str(exe)],
                       check=True, capture_output=True, text=True)
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=5,
                                env={"N64C_LOCKSTEP": "1", "N64C_LOCKSTEP_DIR": str(self.path / "lockstep")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boot-lockstep:", result.stderr)
        points = [json.loads(line) for line in (self.path / "lockstep/lockstep.jsonl").read_text().splitlines()]
        self.assertEqual([p["order"] for p in points if p["k"] == "call"], [1, 1, 2])

    @unittest.skipUnless(args.m32, "run-on returns from frames on i386 only")
    def test_run_on_lists_every_failure_and_goes_on(self):
        # One boot meets an unlinked call, a NULL read, a divide by zero, a spin, an uncovered DMA and a refused
        # task. Run-on records each once, in boot order, and every caller resumes with its own callee-saved registers.
        (self.path / "game.c").write_text(RUN_ON_GAME)
        (self.path / "stub.c").write_text('extern long long n64c_boot_missing(const char *);\n'
                                          'long long unlinked(void) { return n64c_boot_missing("unlinked"); }\n')
        (self.path / "table.c").write_text(
            '#include "trace.h"\nconst N64cTraceEntry n64c_trace_entries[] = {\n'
            '    {1, 0x80000010u, 0, {"keep"}}, {2, 0x80000020u, 0, {"crash"}}, {3, 0x80000030u, 0, {"spin"}},\n'
            '    {4, 0x80000040u, 0, {"never"}}};\nconst unsigned n64c_trace_count = 4;\n')
        (self.path / "shim.c").write_text(RUN_ON_SHIM)
        shim = ROOT.parent
        objects = []
        game = ["-O2", "-finstrument-functions", "-fno-omit-frame-pointer"]
        for source, extra in [(self.path / "game.c", game), (self.path / "stub.c", game), (ROOT / "trace.c", ["-O2"]),
                              (ROOT / "runon.c", ["-O2"]), (ROOT / "missing.c", ["-O2"]),
                              (self.path / "table.c", []), (self.path / "shim.c", [])]:
            obj = self.path / (source.stem + ".o")
            subprocess.run([args.compiler, "-m32", "-Wall", "-Werror", *extra, "-I" + str(ROOT), "-I" + str(shim),
                            "-c", str(source), "-o", str(obj)], check=True, capture_output=True, text=True)
            objects.append(str(obj))
        exe = self.path / "runon"
        subprocess.run([args.compiler, "-m32", "-no-pie", *objects, "-lpthread", "-o", str(exe)],
                       check=True, capture_output=True, text=True)
        env = {"N64C_RUN_ON": "1", "N64C_RUN_ON_HANG": "1", "PATH": "/usr/bin:/bin"}
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env)
        lines = [line for line in result.stderr.splitlines() if not line.startswith("boot-step: ")]
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(result.stdout.split(), ["unlinked=0", "keep=15", "div=0", "spin=0", "after=9"],
                         result.stderr)
        got = [" ".join(line.split()[1:3]) for line in lines if line.startswith("boot-diag: ") and " at=" in line]
        self.assertEqual(got[0], "missing unlinked")
        self.assertTrue(got[1].startswith("fault SIGSEGV@crash"), got)
        self.assertTrue(got[2].startswith("fault SIGFPE@divide"), got)
        self.assertEqual(got[3:], ["hang spin", "layout dma-rom-0x00001000", "stop rdp-buffer"])
        self.assertIn("in=caller<keep<run", next(line for line in lines if "missing unlinked" in line))
        self.assertIn("return=crash", result.stderr)
        self.assertIn("boot-diag: end 6 failures (1 missing, 1 stop, 0 code-pointer, 2 fault, 1 layout, 1 hang), "
                      "census 3/4", result.stderr)
        # Without N64C_RUN_ON the same binary stops at the first failure, as the fail-fast walk and the gate need.
        plain = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(plain.returncode, 78)
        self.assertIn("boot-missing: unlinked", plain.stderr)
        self.assertNotIn("boot-diag:", plain.stderr)
RUN_ON_GAME = r"""
#include <stdio.h>
extern long long unlinked(void);
#define NI __attribute__((noinline))
volatile int zero;
NI int caller(void) { return (int)unlinked(); }
/* Uses every callee-saved register before it faults, so a resumed caller proves they were restored. */
NI int crash(int *p, int n) { int s = 0; for (int i = 0; i < n; i++) s += p[i] * (i + 1) ^ (s >> 3); return s; }
NI int keep(int x) { int a = x * 3, b = x + 5, c = x ^ 7; printf("unlinked=%d\n", caller());
    int r = crash((int *)(long)zero, 40 + x); return a + b + c + r - 9; }
NI int divide(int a, int b) { return a / b; }
NI int spin(void) { while (!zero) { } return zero; } /* the breaker makes it return 0 */
NI int run(void) { int k = keep(4); printf("keep=%d\n", k); printf("div=%d\n", divide(7, zero));
    printf("spin=%d\n", spin()); return 9; }
"""
RUN_ON_SHIM = r"""
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include "n64c_diag.h"
#include "trace.h"
/* The n64compat parts runon.c reads, and the SIGSEGV owner (n64c_memory.c) that hands host faults to run-on. */
int n64c_pi_origin(const void *ram, unsigned *dev) { (void)ram; (void)dev; return 0; }
unsigned n64c_memory_physical(const void *pointer) { return (unsigned)(unsigned long)pointer; }
static void segv(int n, siginfo_t *info, void *context)
{
    if (!n64c_diag_fault(n, info, context)) signal(SIGSEGV, SIG_DFL);
}
extern int run(void);
int main(void)
{
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_sigaction = segv;
    action.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &action, NULL);
    n64c_trace_init();
    n64c_runon_init();
    int after = run();
    char buffer[32];
    n64c_diag_layout("dma", 0x1000, 0x20, buffer);
    if (n64c_diag_stop && !n64c_diag_stop("rdp-buffer", 1)) return 1;
    printf("after=%d\n", after);
    fflush(stdout);
    return n64c_runon_end(0);
}
"""
unittest.main(argv=[sys.argv[0]])
