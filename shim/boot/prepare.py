# SPDX-License-Identifier: GPL-3.0-or-later
"""Preprocess original C and generate stops for its selected INCLUDE_ASMs.

--map OLD=NEW rewrites a path prefix in the translation unit's line markers and __FILE__ (the source root and the
adapter of this build tree), so every build tree emits the same .i and the compiler cache serves all of them.
--cache DIR keeps each result under a key of this script, the compiler, the mapped flags and the source, checked
against the content of every file the preprocessor read (the source and its headers), so a fresh build tree reuses
another tree's preprocessing instead of repeating it."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import portdir  # noqa: E402

IDENT = r"[A-Za-z_][A-Za-z_0-9]*"


def markers(source, prefix):
    # Match the complete declaration emitted by native.h, not comments,
    # strings, inactive version branches or arbitrary symbol substrings.
    clean, quote, escaped = [], None, False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif ord(char) == 92:
                escaped = True
            elif char == quote:
                quote = None
            clean.append(" ")
        elif char in (chr(34), chr(39)):
            quote = char
            clean.append(" ")
        else:
            clean.append(char)
    source = "".join(clean)
    kind = r"void\s+" if prefix == "missing" else r"char\s+"
    suffix = r"\s*\(\s*void\s*\)\s*;" if prefix == "missing" else r"\s*;"
    return sorted(set(re.findall(r"\bextern\s+" + kind +
        r"n64c_" + prefix + r"_marker_(" + IDENT + ")" + suffix, source)))


def known_fallbacks():
    """Sources recorded as known NON_MATCHING compile failures: each may still contribute its matched C."""
    path = portdir.fact("known-fallback-units.txt")
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}
def listed(source, units):
    return any(str(source).endswith("/" + unit) for unit in units)
def mapped(text, maps):
    for old, new in maps:
        text = text.replace(old, new)
    return text
def map_markers(text, maps):
    # Line markers only: `# LINE "PATH" FLAGS`; __FILE__ is mapped by -fmacro-prefix-map.
    return re.sub(r'(?m)^(# \d+ ")([^"]*)"', lambda m: m.group(1) + mapped(m.group(2), maps) + '"', text)
def dependencies(depfile):
    # gcc -MD: `TARGET: DEP DEP \` continued over lines; the first dependency is the source itself.
    text = depfile.read_text().replace("\\\n", " ")
    return text.split(":", 1)[1].split()
def file_hash(path):
    with open(path, "rb") as stream:  # file_digest reads in large blocks: about half the cost of read_bytes + sha256
        return hashlib.file_digest(stream, "sha256").hexdigest()
def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
class Cache:
    """Results keyed like a compiler cache in direct mode: a manifest per (script, compiler, flags, source) lists
    the files each stored result read with their content hashes; a result is reused when all still match."""
    LIMIT = 4 << 30  # bytes; past it the least recently used results go
    def __init__(self, root, compiler, source, flags, maps):
        self.root, self.maps = pathlib.Path(root), maps
        real = pathlib.Path(shutil.which(compiler) or compiler).resolve()
        stat = real.stat()
        key = json.dumps([file_hash(__file__), str(real), stat.st_size, stat.st_mtime_ns,
                          [mapped(f, maps) for f in flags], mapped(str(source), maps), file_hash(source)])
        self.key = hashlib.sha256(key.encode()).hexdigest()
        self.manifest = self.root / self.key[:2] / (self.key + ".json")
    def unmap(self, path):
        for old, new in self.maps:
            if path.startswith(new):
                return old + path[len(new):]
        return path
    def lookup(self):
        try:
            entries = json.loads(self.manifest.read_text())
        except (OSError, ValueError):
            return None
        for entry in entries:
            try:
                if all(file_hash(self.unmap(path)) == digest for path, digest in entry["deps"]):
                    blob = self.root / entry["result"][:2] / (entry["result"] + ".z")
                    result = json.loads(zlib.decompress(blob.read_bytes()))
                    os.utime(blob)
                    os.utime(self.manifest)
                    return result
            except (OSError, ValueError, zlib.error):
                continue
        return None
    def store(self, result, deps):
        entry = {"deps": [[mapped(path, self.maps), file_hash(path)] for path in deps]}
        name = hashlib.sha256(json.dumps([self.key, entry["deps"]]).encode()).hexdigest()
        entry["result"] = name
        write_atomic(self.root / name[:2] / (name + ".z"), zlib.compress(json.dumps(result).encode(), 1))
        try:
            entries = json.loads(self.manifest.read_text())
        except (OSError, ValueError):
            entries = []
        entries = [entry] + [e for e in entries if e["result"] != name][:7]
        write_atomic(self.manifest, json.dumps(entries).encode())
        if int(name[:4], 16) % 64 == 0:
            self.trim()
    def trim(self):
        files = sorted((f.stat().st_mtime, f.stat().st_size, f) for f in self.root.glob("*/*") if f.is_file())
        total = sum(size for _, size, _ in files)
        for _, size, path in files:
            if total <= self.LIMIT * 4 // 5:
                break
            path.unlink(missing_ok=True)
            total -= size
def prepare(compiler, source, output, stubs, inventory, depfile, flags, maps=(), cache=None, fallbacks=()):
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = list(flags) + [f"-fmacro-prefix-map={old}={new}" for old, new in maps]
    store = Cache(cache, compiler, source, flags, maps) if cache else None
    mode = "NON_MATCHING" if "-DNON_MATCHING" in flags else "matched"
    result = store.lookup() if store else None
    if result:
        record = result["inventory"]
        allowed = listed(source, fallbacks) and record.get("mode") == "matched"
        if not allowed and (record.get("mode") != mode or record.get("non_matching_errors")):
            sys.stderr.write(f"decomp-prepare: unit {source.name} ({source}): refusing cached fallback "
                             f"or invalid result (requested {mode}, cached {record.get('mode')!r})\n")
            return 1
        sys.stderr.write(result["stderr"])
        output.write_text(result["i"])
        stubs.write_text(result["stubs"])
        inventory.write_text(json.dumps(dict(result["inventory"], source=str(source)), sort_keys=True) + "\n")
        deps = [store.unmap(path) for path, _ in result["deps"]]
        depfile.write_text(f"{output}: " + " \\\n ".join(deps) + "\n")
        return 0
    # NON_MATCHING is an input contract. A failed body never becomes a successful matched-only unit with generated
    # stops, unless the unit is a recorded known failure (known-fallback-units.txt): then its matched C is kept and
    # the diagnostics stay in the inventory. Any other unit fails, including via the cache.
    rejected = []
    if mode == "NON_MATCHING":
        check = subprocess.run([compiler, *flags, "-x", "c", "-fsyntax-only", str(source)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check.returncode:
            rejected = [line for line in check.stderr.splitlines() if "error:" in line]
            matched = [flag for flag in flags if flag != "-DNON_MATCHING"]
            if listed(source, fallbacks):
                again = subprocess.run([compiler, *matched, "-x", "c", "-fsyntax-only", str(source)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if not listed(source, fallbacks) or again.returncode:
                sys.stderr.write(f"decomp-prepare: unit {source.name} ({source}): NON_MATCHING compile failed\n")
                sys.stderr.write(check.stderr)
                return check.returncode
            flags, mode = matched, "matched"
            sys.stderr.write(f"decomp-prepare: {source}: known NON_MATCHING failure; using matched C with stops\n")
    command = [compiler, *flags, "-x", "c", "-E", "-MD", "-MF", str(depfile),
               "-MT", str(output), str(source)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if result.returncode:
        sys.stderr.write(f"decomp-prepare: unit {source.name} ({source}): preprocessing failed\n")
        sys.stderr.write(result.stderr)
        return result.returncode
    missing = markers(result.stdout, "missing")
    data = markers(result.stdout, "data")
    # No definitions are fabricated for data macros. They remain unresolved
    # until the ROM loader has a qualified layout and relocation description.
    declarations = ["/* Generated missing-function stops; no original code. */",
        "extern long long n64c_boot_missing(const char *);"]
    declarations += ['long long ' + name + '(void) { return n64c_boot_missing("' + name + '"); }'
                     for name in missing]
    text = map_markers(result.stdout, maps)
    record = {"missing": missing, "data": data, "mode": mode, "non_matching_errors": rejected[:50]}
    output.write_text(text)
    stubs.write_text("\n".join(declarations) + "\n")
    inventory.write_text(json.dumps(dict(record, source=str(source)), sort_keys=True) + "\n")
    if store:
        try:
            store.store({"i": text, "stubs": "\n".join(declarations) + "\n", "inventory": record,
                         "stderr": "", "deps": [[mapped(p, maps), ""] for p in dependencies(depfile)]},
                        dependencies(depfile))
        except OSError as error:  # a full or read-only cache never fails the build
            sys.stderr.write(f"decomp-prepare: cache not written: {error}\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("compiler", "source", "output", "stubs", "inventory", "depfile"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="path prefix to rewrite in line markers and __FILE__ (repeatable)")
    parser.add_argument("--cache", default="", help="shared result cache directory (empty: none)")
    parser.add_argument("flags", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
    maps = [tuple(m.split("=", 1)) for m in args.map]
    return prepare(args.compiler, pathlib.Path(args.source), pathlib.Path(args.output),
                   pathlib.Path(args.stubs), pathlib.Path(args.inventory),
                   pathlib.Path(args.depfile), flags, maps, args.cache or None, known_fallbacks())


if __name__ == "__main__":
    sys.exit(main())
