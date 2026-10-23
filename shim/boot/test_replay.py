# SPDX-License-Identifier: GPL-3.0-or-later
"""Build replay.c against a fixture image and run recorded cases: inputs, returns of every kind, outputs, faults."""
import argparse
import pathlib
import subprocess
import tempfile
import unittest

parser = argparse.ArgumentParser()
parser.add_argument("--compiler", required=True)
parser.add_argument("--work", type=pathlib.Path, required=True)
parser.add_argument("--m32", action="store_true")
args = parser.parse_args()
args.work.mkdir(parents=True, exist_ok=True)
ROOT = pathlib.Path(__file__).resolve().parent
SHIM = ROOT.parent

DRIVER = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "n64c_memory.h"
int n64c_replay_run(const char *plan);
static unsigned char ram[0x10000];
void *n64c_memory_ram(u32 physical, u32 bytes) { return (physical + bytes <= sizeof ram) ? ram + physical : 0; }
/* a "game": reads and writes the image, as native code does */
unsigned sum3(unsigned a, unsigned b, unsigned c) { return a + b * 10 + c * 100; }
unsigned stackarg(unsigned a, unsigned b, unsigned c, unsigned d, unsigned e) { return e - a - b - c - d; }
unsigned long long wide(unsigned a) { return 0x100000000ull * 5 + a; }
float half(float x) { return x / 2.0f; }
double twice(double x) { return x * 2.0; }
unsigned load_store(unsigned p) {
    unsigned v = *(unsigned *)(ram + p);
    unsigned char b = *(unsigned char *)(ram + p + 4);
    *(unsigned short *)(ram + p + 8) = (unsigned short)(v + b);
    *(unsigned char *)(ram + p + 10) = b;
    return v;
}
unsigned crash(void) { return *(volatile unsigned *)0; }
unsigned spin(void) { for (;;) { } }
int main(int argc, char **argv) {
    (void)argc;
    printf("%x %x %x %x %x %x %x %x %x %x\n", (unsigned)(uintptr_t)sum3, (unsigned)(uintptr_t)stackarg,
           (unsigned)(uintptr_t)wide, (unsigned)(uintptr_t)half, (unsigned)(uintptr_t)twice,
           (unsigned)(uintptr_t)load_store, (unsigned)(uintptr_t)crash, (unsigned)(uintptr_t)spin, 0, 0);
    fflush(stdout);
    return argc > 1 ? n64c_replay_run(argv[1]) : 0;
}
'''


class ReplayTests(unittest.TestCase):
    def test_cases(self):
        with tempfile.TemporaryDirectory(dir=args.work) as tmp:
            path = pathlib.Path(tmp)
            (path / "driver.c").write_text(DRIVER)
            exe = path / "driver"
            flags = ["-m32"] if args.m32 else []
            subprocess.run([args.compiler, "-O1", "-Wall", "-Werror", "-fno-omit-frame-pointer", "-no-pie", *flags,
                            "-I", str(SHIM), str(ROOT / "replay.c"), str(path / "driver.c"), "-o", str(exe)],
                           check=True, capture_output=True, text=True)
            addrs = [int(x, 16) for x in subprocess.run([exe], capture_output=True, text=True, check=True).stdout.split()]
            sum3, stackarg, wide, half, twice, load_store, crash, spin = addrs[:8]
            plan = f"""out {path / 'out.txt'}
timeout 1
case sum
fn {sum3:x} i
args 1 2 3
go
case stack
fn {stackarg:x} i
args 1 2 3 4 64
go
case wide
fn {wide:x} q
args 7
go
case half
fn {half:x} f
args 40a00000
go
case twice
fn {twice:x} d
args 0 3ff80000
go
case mem
fn {load_store:x} i
args 100
in 100 4 12345678
in 104 1 7
want 108 2
want 10a 1
go
case again
fn {load_store:x} i
args 100
want 108 2
go
case nomem
fn {sum3:x} i
args 0
in 20000 4 1
go
case crash
fn {crash:x} i
go
case spin
fn {spin:x} i
go
"""
            (path / "plan.txt").write_text(plan)
            done = subprocess.run([exe, str(path / "plan.txt")], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = (path / "out.txt").read_text().splitlines()
            by = {}
            for line in lines:
                parts = line.split()
                if parts[0] == "res":
                    by[parts[1]] = parts[2:]
                    cur = parts[1]
                else:
                    by[cur].append(parts)
            self.assertEqual(by["sum"][0], "ok")
            self.assertEqual(int(by["sum"][1], 16), 0x1 + 0x2 * 10 + 0x3 * 100)
            self.assertEqual(int(by["stack"][1], 16), 0x64 - 1 - 2 - 3 - 4)
            self.assertEqual((int(by["wide"][1], 16), int(by["wide"][2], 16)), (7, 5))
            self.assertEqual(int(by["half"][3], 16), 0x40200000)  # 2.5f
            self.assertEqual(int(by["twice"][3], 16), 0x4008000000000000)  # 3.0
            self.assertEqual(int(by["mem"][1], 16), 0x12345678)
            self.assertEqual(by["mem"][4:], [["got", "108", "2", hex(0x5678 + 7)[2:]], ["got", "10a", "1", "7"]])
            self.assertEqual(by["again"][4:][0][3], "0")  # the next case starts from the loaded image, not the last case's stores
            self.assertEqual(by["nomem"][:2], ["nomem", "20000"])
            self.assertEqual(by["crash"][0], "crash")
            self.assertEqual(by["crash"][1], "11")  # SIGSEGV
            self.assertEqual(by["spin"], ["timeout"])


if __name__ == "__main__":
    import sys

    sys.argv = [sys.argv[0]]
    unittest.main()
