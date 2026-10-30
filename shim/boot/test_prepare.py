# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise real preprocessing, compilation, ABI calls and terminal stops."""
import argparse
import contextlib
import io
import importlib.util
import json
import os
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

    def test_failed_header_records_dependencies_for_repair(self):
        header = self.path / "shared.h"
        header.write_text("struct Record { int team; int team; };\n")
        source = self.path / "unit.c"
        source.write_text('#include "shared.h"\nint actual(void) { return 7; }\n')
        files = [self.path / name for name in ("unit.i", "unit.stubs.c", "unit.json", "unit.d")]
        with contextlib.redirect_stderr(io.StringIO()):
            result = module.prepare(args.compiler, source, *files, [*self.flags, "-DNON_MATCHING"])
        self.assertNotEqual(result, 0)
        self.assertIn(str(header), files[-1].read_text())
        self.assertFalse(files[0].exists())
        header.write_text("struct Record { int characterId; int team; };\n")
        self.assertEqual(module.prepare(args.compiler, source, *files, [*self.flags, "-DNON_MATCHING"]), 0)
        self.assertIn("characterId", files[0].read_text())

    def test_uncompilable_nonmatching_fails_without_stops(self):
        src = self.path / "conflict.c"
        src.write_text("""
extern int shared;
int matched(void) { return 7; }
#ifdef NON_MATCHING
extern char shared;
int fuzzy(void) { return shared; }
#else
INCLUDE_ASM("text", fuzzy);
#endif
""")
        files = [self.path / n for n in ("source.i", "stubs.c", "inventory.json", "source.d")]
        cache = self.path / "cache"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = module.prepare(args.compiler, src, *files,
                self.flags + ["-DNON_MATCHING", "-include", str(ROOT / "native.h")], cache=cache)
        self.assertNotEqual(status, 0)
        self.assertIn("conflicting types", stderr.getvalue())
        self.assertIn("unit conflict.c", stderr.getvalue())
        self.assertIn(str(src), stderr.getvalue())
        self.assertFalse(any(f.exists() for f in files[:-1]))
        self.assertIn(str(src), files[-1].read_text())
        self.assertEqual(list(cache.rglob("*")), [])

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

    def test_cache_serves_another_tree_and_follows_headers(self):
        cache = self.path / "cache"
        def tree(name):
            root = self.path / name
            (root / "include").mkdir(parents=True)
            (root / "include" / "h.h").write_text("#define VALUE 5\n")
            (root / "u.c").write_text('#include "h.h"\nint value(void) { return VALUE; }\nconst char *f = __FILE__;\n')
            files = [root / n for n in ("u.i", "u.stubs.c", "u.json", "u.d")]
            def run():
                return module.prepare(args.compiler, root / "u.c", *files, self.flags + ["-I" + str(root / "include")],
                                      [(str(root), "tree")], cache)
            return root, files, run
        a, files_a, run_a = tree("a")
        self.assertEqual(run_a(), 0)
        b, files_b, run_b = tree("b")
        real = module.subprocess.run
        module.subprocess.run = lambda *a, **k: self.fail("a cached unit runs no compiler")
        try:
            self.assertEqual(run_b(), 0)
        finally:
            module.subprocess.run = real
        self.assertEqual(files_a[0].read_text(), files_b[0].read_text(), "both trees emit the same unit")
        self.assertNotIn(str(a), files_a[0].read_text())
        self.assertIn('"tree/u.c"', files_b[0].read_text())
        self.assertIn(str(b / "include" / "h.h"), files_b[3].read_text(), "the restored depfile names this tree")
        self.assertEqual(json.loads(files_b[2].read_text())["source"], str(b / "u.c"))
        (b / "include" / "h.h").write_text("#define VALUE 6\n")
        self.assertEqual(run_b(), 0)
        self.assertIn("return 6", files_b[0].read_text(), "a changed header misses the cache")

    def test_native_varargs(self):
        files, data = self.generate("""
#include <stdarg.h>
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


class ObjectCacheTests(unittest.TestCase):
    """unitstep.py --objcache: a restored unit runs no step; an edited header or another tree's paths behave."""
    STEP = ("import pathlib, sys\n"
            "out = pathlib.Path(sys.argv[1])\n"
            "out.write_text(open(sys.argv[2]).read())\n"
            "pathlib.Path(sys.argv[3]).write_text(str(out) + ': ' + sys.argv[2] + '\\n')\n"
            "(out.parent / 'ran').write_text('x')\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=args.work)
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        (self.path / "cache").mkdir()
        (self.path / "src").mkdir()

    def unit(self, tree, header="1"):
        build = self.path / tree
        build.mkdir(exist_ok=True)
        header_file = self.path / "src" / "h.h"
        if not header_file.exists() or header_file.read_text() != header:  # an untouched header keeps its signature
            header_file.write_text(header)
        step = build / "step.py"
        step.write_text(self.STEP)
        obj = build / "u.o"
        (build / "ran").unlink(missing_ok=True)
        (build / "u.i").write_text("stale")
        subprocess.run([sys.executable, "-I", str(ROOT / "unitstep.py"), "--base", str(build / "u"), "--source", "u",
                        "--objcache", str(self.path / "cache"), "--root", str(build),
                        "--map", f"{self.path}/src=SRC", "--out", str(obj), "--drop", str(build / "u.i"),
                        "--depfile", str(build / "u.d"), "--dep-target", str(obj), "--",
                        sys.executable, str(step), str(obj), str(header_file), str(build / "u.d")], check=True)
        return build, obj.read_text(), (build / "ran").exists()

    def test_restore_invalidate_share(self):
        build, text, ran = self.unit("a")
        self.assertEqual((text, ran), ("1", True))
        build, text, ran = self.unit("a")
        self.assertEqual((text, ran), ("1", False))
        self.assertFalse((build / "u.i").exists())
        self.assertIn(str(self.path / "src" / "h.h"), (build / "u.d").read_text())
        self.assertTrue((build / "u.d").read_text().startswith(f"{build / 'u.o'}:"))
        build, text, ran = self.unit("a", header="22")
        self.assertEqual((text, ran), ("22", True))
        build, text, ran = self.unit("b", header="22")
        self.assertEqual((text, ran), ("22", False))  # another tree: the same key through --root and --map

    GCNO_STEP = ("import os, pathlib, sys\n"
                 "out = pathlib.Path(sys.argv[1])\n"
                 "out.write_text(open(sys.argv[2]).read())\n"
                 "cwd = os.getcwd().encode() + b'\\0'\n"
                 "head = b'oncg' + b'*24B' + b'stmp' + bytes(4) + len(cwd).to_bytes(4, 'little')\n"
                 "out.with_suffix('.gcno').write_bytes(head + cwd + b'rest')\n"
                 "pathlib.Path(sys.argv[3]).write_text(str(out) + ': ' + sys.argv[2] + '\\n')\n"
                 "(out.parent / 'ran').write_text('x')\n")

    def gcno_restored(self, trees):
        """Builds the coverage unit in each of TREES (name -> directory, the second restored from the first's cache)
        and returns what the second tree's .gcno holds beside whether its step ran."""
        header_file = self.path / "src" / "h.h"
        header_file.write_text("1")
        found = []
        for build in trees:
            (build / "step.py").write_text(self.GCNO_STEP)
            obj = build / "u.o"
            subprocess.run([sys.executable, "-I", str(ROOT / "unitstep.py"), "--base", str(build / "u"), "--source",
                            "u", "--objcache", str(self.path / "cache"), "--root", str(build),
                            "--map", f"{self.path}/src=SRC", "--out", str(obj), "--out", str(build / "u.gcno"),
                            "--depfile", str(build / "u.d"), "--dep-target", str(obj), "--", sys.executable,
                            str(build / "step.py"), str(obj), str(header_file), str(build / "u.d")],
                           check=True, cwd=build)
            found.append(((build / "ran").exists(), (build / "u.gcno").read_bytes()))
        return found

    @staticmethod
    def gcno_expect(tree):
        """The .gcno a restore leaves in TREE: the compile directory is the canonical path, which is what getcwd
        answers however the directory was reached (bind mount, symlink)."""
        cwd = os.path.realpath(tree).encode() + b"\0"
        return b"oncg*24Bstmp" + bytes(4) + len(cwd).to_bytes(4, "little") + cwd + b"rest"

    def test_a_coverage_units_gcno_is_restored_naming_the_tree_it_is_restored_in(self):
        """PF3: a coverage build kept the object cache off (a restored object had no .gcno) and cost 642-678 CPU s a
        build. The .gcno is an --out; restored in another tree it names that tree as its compile directory."""
        trees = [self.path / "a", self.path / "b"]
        for tree in trees:
            tree.mkdir()
        first, second = self.gcno_restored(trees)
        self.assertTrue(first[0])
        self.assertFalse(second[0])  # restored, no step ran
        self.assertEqual(second[1], self.gcno_expect(trees[1]))

    def test_a_gcno_restored_through_a_symlinked_directory_names_its_canonical_path(self):
        """The tree is reached through a symlink (as /mnt/devstorage is on the rig, a bind mount of another path):
        the logical path differs from the canonical one getcwd answers, and the .gcno must hold the canonical one."""
        real = self.path / "real"
        (real / "a").mkdir(parents=True)
        (real / "b").mkdir()
        link = self.path / "link"
        link.symlink_to(real)
        trees = [link / "a", link / "b"]
        first, second = self.gcno_restored(trees)
        self.assertNotEqual(os.path.realpath(trees[1]), str(trees[1]))
        self.assertFalse(second[0])
        self.assertEqual(second[1], self.gcno_expect(trees[1]))
        self.assertNotIn(str(link).encode(), second[1])  # the logical path is not what the .gcno names

    def test_restore_answers_from_signatures(self):
        """A restore of an unchanged dependency stats it and never reads the digest memo; touching the file's
        content (same size, new mtime) still goes through the memo and the hash."""
        memo = self.path / "cache" / "digests.memo"
        self.unit("a")
        memo.write_text("not json")  # a restore that read the memo would start from an empty one and rewrite it
        build, text, ran = self.unit("a")
        self.assertEqual((text, ran), ("1", False))
        self.assertEqual(memo.read_text(), "not json")
        header = self.path / "src" / "h.h"
        os.utime(header, ns=(1, 1))  # same bytes, other signature: hashed, still the same content
        self.assertEqual(self.unit("a", header="1")[2], False)
        self.assertEqual(self.unit("a", header="2")[2], True)  # same size, other content: rebuilt

    def test_key_text_matches_json_and_old_manifest_is_read(self):
        """The cache key is hashed from a hand-written json.dumps (no json import); entries written as JSON by
        earlier builds still restore, and are rewritten in the marshal format."""
        spec = importlib.util.spec_from_file_location("boot_unitstep_keys", ROOT / "unitstep.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        value = [1, ["a b", "caf\u00e9 \"q\" \\ \n", "/x/y.h"], ["0f" * 32], ["/usr/bin/cc", 123, 456789], None, []]
        self.assertEqual(module.dumps(value), json.dumps(value))
        build, text, ran = self.unit("a")
        manifest = next((self.path / "cache").glob("??/*.json"))
        entries = module.load_manifest(manifest)[0]
        self.assertTrue(manifest.read_bytes().startswith(module.MAGIC))
        old = [dict(e, files=[None if f is None else __import__("base64").b64encode(f).decode() for f in e["files"]])
               for e in entries]
        for entry in old:
            del entry["sigs"]
        manifest.write_text(json.dumps(old))
        build, text, ran = self.unit("a")
        self.assertEqual((text, ran), ("1", False))
        self.assertTrue(manifest.read_bytes().startswith(module.MAGIC))
        self.assertIn("sigs", module.load_manifest(manifest)[0][0])


unittest.main(argv=["test_prepare"], verbosity=2)
