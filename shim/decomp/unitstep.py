# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a link-all archive unit's prepare + compile steps: a failure leaves empty objects and a .failed marker, so one
unit that does not compile never blocks the archive or the link (linkall.py). Kept apart from linkall.py: every
archive unit's command depends on this file, so a change to the archive or autogen logic recompiles nothing.
  unitstep.py --base BASE [--source RELATIVE] [--strict] [OBJECT CACHE OPTIONS] -- step --- step ...

Object cache (--objcache DIR): a unit whose steps, tools and every file the preprocessor read are unchanged is restored
from DIR without running a single step, so a fresh build tree costs one small read per unit (no compiler cache spawns,
no preprocessing, no .i written into the tree). Entries sit under a key of the normalised steps (--root and --map
paths rewritten, so every tree shares them), the compiler and the scripts the steps run, and list each header with its
content hash; a header's hash is remembered per tree (ROOT/unitstep-digests.json) by size and mtime, so a header
shared by thousands of units is read once per tree, not once per unit.
  --root BUILD  --map OLD=NEW (repeatable)  --compiler PATH  --out FILE (repeatable: the files restored)
  --drop FILE (repeatable: removed on a restore, e.g. a stale .i)  --depfile FILE  --dep-target FILE
Only --out files, the depfile and --drop are touched: an entry holds each --out file's bytes or its absence."""
import binascii
import hashlib
import json
import os
import sys

VERSION = 1
LIMIT = 2 << 30  # bytes of cached entries; past it the least recently used go


def digest_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Digests:
    """Content hash of each file by (path, size, mtime): one hash per file per tree instead of one per unit."""

    def __init__(self, memo):
        self.memo, self.fresh = memo, False
        try:
            with open(memo) as stream:
                self.known = json.load(stream)
        except (OSError, ValueError):
            self.known = {}
        self.added = {}

    def of(self, path):
        stat = os.stat(path)
        sig = [stat.st_size, stat.st_mtime_ns]
        entry = self.known.get(path)
        if entry and entry[:2] == sig:
            return entry[2]
        value = digest_file(path)
        self.added[path] = [*sig, value]
        return value

    def save(self):
        if not self.added:
            return
        try:
            with open(self.memo) as stream:
                merged = json.load(stream)
        except (OSError, ValueError):
            merged = {}
        merged.update(self.added)
        temporary = f"{self.memo}.{os.getpid()}.tmp"
        try:
            with open(temporary, "w") as stream:
                json.dump(merged, stream)
            os.replace(temporary, self.memo)
        except OSError:
            pass


def mapped(text, maps):
    for old, new in maps:
        text = text.replace(old, new)
    return text


def unmapped(path, maps):
    for old, new in maps:
        if path.startswith(new):
            return old + path[len(new):]
    return path


def dependencies(depfile):
    """gcc -MD: `TARGET: DEP DEP \\` continued over lines; the first dependency is the source itself."""
    with open(depfile) as stream:
        text = stream.read().replace("\\\n", " ")
    return text.split(":", 1)[1].split()


class ObjectCache:
    def __init__(self, opts, commands):
        self.opts, self.maps = opts, opts["maps"]
        root = opts["root"]
        words = [mapped(word.replace(root, "@B") if root else word, self.maps) for command in commands for word in command]
        scripts = [word for command in commands for word in command if word.endswith(".py") and os.path.isfile(word)]
        self.digests = Digests(os.path.join(root, "unitstep-digests.json") if root else os.devnull)
        tool = opts.get("compiler")
        stat = os.stat(os.path.realpath(tool)) if tool else None
        key = json.dumps([VERSION, words, [self.digests.of(path) for path in sorted(set(scripts))],
                          [os.path.realpath(tool), stat.st_size, stat.st_mtime_ns] if tool else None])
        self.key = hashlib.sha256(key.encode()).hexdigest()
        self.manifest = os.path.join(opts["objcache"], self.key[:2], self.key + ".json")

    def restore(self):
        try:
            with open(self.manifest) as stream:
                entries = json.load(stream)
        except (OSError, ValueError):
            return False
        for entry in entries:
            try:
                if any(self.digests.of(unmapped(path, self.maps)) != value for path, value in entry["deps"]):
                    continue
            except OSError:
                continue
            outs = self.opts["outs"]
            for path, text in zip(outs, entry["files"]):
                if text is None:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as stream:
                        stream.write(binascii.a2b_base64(text))
            for path in self.opts["drops"]:
                if os.path.exists(path):
                    os.unlink(path)
            if self.opts.get("depfile"):
                target = self.opts.get("dep_target") or outs[0]
                with open(self.opts["depfile"], "w") as stream:
                    stream.write(f"{target}: " + " \\\n ".join(unmapped(path, self.maps) for path, _ in entry["deps"]) + "\n")
            self.digests.save()
            try:
                os.utime(self.manifest)
            except OSError:
                pass
            return True
        return False

    def store(self):
        depfile = self.opts.get("depfile")
        if not depfile or not os.path.exists(depfile):
            return
        try:
            deps = [[mapped(path, self.maps), self.digests.of(path)] for path in dependencies(depfile)]
            files = []
            for path in self.opts["outs"]:
                if os.path.exists(path):
                    with open(path, "rb") as stream:
                        files.append(binascii.b2a_base64(stream.read(), newline=False).decode())
                else:
                    files.append(None)
            try:
                with open(self.manifest) as stream:
                    entries = json.load(stream)
            except (OSError, ValueError):
                entries = []
            entry = {"deps": deps, "files": files}
            entries = [entry] + [e for e in entries if e["deps"] != deps][:3]
            os.makedirs(os.path.dirname(self.manifest), exist_ok=True)
            temporary = f"{self.manifest}.{os.getpid()}.tmp"
            with open(temporary, "w") as stream:
                json.dump(entries, stream)
            os.replace(temporary, self.manifest)
            if int(self.key[:4], 16) % 128 == 0:
                self.trim()
        except OSError as error:  # a full or read-only cache never fails the build
            sys.stderr.write(f"unitstep: object cache not written: {error}\n")
        self.digests.save()

    def trim(self):
        import glob
        files = sorted((os.stat(f).st_mtime, os.stat(f).st_size, f)
                       for f in glob.glob(os.path.join(self.opts["objcache"], "*", "*.json")))
        total = sum(size for _, size, _ in files)
        for _, size, path in files:
            if total <= LIMIT * 4 // 5:
                break
            os.unlink(path)
            total -= size


OUTPUT_SUFFIXES = (".i", ".stubs.c", ".json", ".o", ".stubs.o")


def run(command):
    """One step. The prepare.py step runs in this process (a Python start and its imports are paid once per unit
    instead of twice); every other step is its own process."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "prepare.py")
    if command[1:2] == [script] or command[1:3] == ["-I", script]:
        sys.dont_write_bytecode = True
        sys.path.insert(0, here)
        import io
        import prepare
        err = io.StringIO()
        saved = sys.argv, sys.stderr
        sys.argv, sys.stderr = [script, *command[command.index(script) + 1:]], err
        try:
            code = prepare.main()
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else 2
        finally:
            sys.argv, sys.stderr = saved
        return subprocess.CompletedProcess(command, code or 0, "", err.getvalue())
    return subprocess.run(command, capture_output=True, text=True)


def parse(argv):
    opts = {"maps": [], "outs": [], "drops": [], "strict": False, "base": "", "source": "", "root": ""}
    flags = {"--base": "base", "--source": "source", "--objcache": "objcache", "--root": "root",
             "--compiler": "compiler", "--depfile": "depfile", "--dep-target": "dep_target"}
    index = 0
    while index < len(argv) and argv[index] != "--":
        word = argv[index]
        if word == "--strict":
            opts["strict"] = True
        elif word == "--map":
            index += 1
            opts["maps"].append(tuple(argv[index].split("=", 1)))
        elif word == "--out":
            index += 1
            opts["outs"].append(argv[index])
        elif word == "--drop":
            index += 1
            opts["drops"].append(argv[index])
        elif word in flags:
            index += 1
            opts[flags[word]] = argv[index]
        else:
            raise SystemExit(f"unitstep: unknown option {word}")
        index += 1
    commands, current = [], []
    for word in argv[index + 1:]:
        if word == "---":
            commands.append(current)
            current = []
        else:
            current.append(word)
    commands.append(current)
    return opts, commands


def unit(argv):
    """--base BASE [--source RELATIVE] ... -- step --- step ...; steps run in order until one fails."""
    opts, commands = parse(argv)
    cache = ObjectCache(opts, commands) if opts.get("objcache") and opts["outs"] else None
    if cache and cache.restore():
        return 0
    import re
    base = opts["base"]
    marker = base + ".failed"
    for command in commands:
        r = run(command)
        if r.returncode and opts["strict"]:
            sys.stderr.write(r.stdout + r.stderr)
            return r.returncode
        if r.stderr and not r.returncode:
            sys.stderr.write(r.stderr)
        if r.returncode:
            err = next((m for m in re.finditer(r"([^\s:]+):(\d+):\d+: error: (.*)", r.stderr)), None)
            reason = f"{opts['source']}:{err.group(2)} {err.group(3)}" if err else f"{opts['source']} does not compile"
            with open(marker, "w") as stream:
                stream.write(reason + "\n")
            for suffix in OUTPUT_SUFFIXES:
                target = base + suffix
                if suffix in (".o", ".stubs.o") or not os.path.exists(target):
                    with open(target, "wb"):
                        pass
            break
    else:
        if os.path.exists(marker):
            os.unlink(marker)
    depfile, target = opts.get("depfile"), opts.get("dep_target")
    if depfile and target and os.path.exists(depfile):  # the .i is not an output: name the object as the target
        with open(depfile) as stream:
            text = stream.read()
        with open(depfile, "w") as stream:
            stream.write(target + ":" + text.split(":", 1)[1])
    if cache:
        cache.store()
    return 0


if __name__ == "__main__":
    raise SystemExit(unit(sys.argv[1:]))
