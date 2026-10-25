# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a link-all archive unit's prepare + compile steps: a failure leaves empty objects and a .failed marker, so one
unit that does not compile never blocks the archive or the link (linkall.py). Kept apart from linkall.py: every
archive unit's command depends on this file, so a change to the archive or autogen logic recompiles nothing.
  unitstep.py --base BASE [--source RELATIVE] [--strict] [OBJECT CACHE OPTIONS] -- step --- step ...

Object cache (--objcache DIR): a unit whose steps, tools and every file the preprocessor read are unchanged is restored
from DIR without running a single step, so a fresh build tree costs one small read per unit (no compiler cache spawns,
no preprocessing, no .i written into the tree). Entries sit under a key of the normalised steps (--root and --map
paths rewritten, so every tree shares them), the compiler and the scripts the steps run, and list each header with its
content hash; a header's hash is remembered by size and mtime in OBJCACHE/digests.memo (digests.py), so a header
shared by thousands of units is read once per machine, not once per unit or tree.
  --root BUILD  --map OLD=NEW (repeatable)  --compiler PATH  --out FILE (repeatable: the files restored)
  --drop FILE (repeatable: removed on a restore, e.g. a stale .i)  --depfile FILE  --dep-target FILE
Only --out files, the depfile and --drop are touched: an entry holds each --out file's bytes or its absence.
A coverage build (--coverage) lists each step's .gcno as an --out: its flags key it apart from a plain build's entry in
the one cache, and a restored .gcno names the directory it is restored in (the step's cwd), as the compile there would
(gcov resolves the unit's relative source names from it), so a coverage build in another tree restores too."""
import marshal
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from digests import Digests  # noqa: E402

VERSION = 1
MAGIC = b"RWOC1\n"  # a manifest of this format starts with it; anything else is the JSON of before
SMALL = 1 << 20  # a dependency up to this size is hashed in place when its signature differs
LIMIT = 2 << 30  # bytes of cached entries; past it the least recently used go


def sha256_hex(data):
    """sha256 without hashlib (its import costs as much as the rest of a restore): the C module behind it."""
    try:
        from _sha2 import sha256
    except ImportError:
        from hashlib import sha256
    return sha256(data).hexdigest()


def script_digest(path):
    with open(path, "rb") as stream:
        return sha256_hex(stream.read())


def dumps(value):
    """json.dumps of the strings, ints, None and lists of the cache key, byte for byte (so the keys of every entry
    already in the cache stay), without importing json."""
    if value is None:
        return "null"
    if isinstance(value, str):
        from _json import encode_basestring_ascii
        return encode_basestring_ascii(value)
    if isinstance(value, int):
        return repr(value)
    return "[" + ", ".join(dumps(item) for item in value) + "]"


def load_manifest(path):
    """(entries, current): a manifest file is marshal data behind MAGIC, or the JSON older builds wrote (not current)."""
    with open(path, "rb") as stream:
        data = stream.read()
    try:
        if data.startswith(MAGIC):
            return marshal.loads(data[len(MAGIC):]), True
        import json
        return json.loads(data), False
    except (ValueError, EOFError, TypeError):
        raise ValueError(path)


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
        self.digests = Digests(os.path.join(opts["objcache"], "digests.memo"))
        tool = opts.get("compiler")
        stat = os.stat(os.path.realpath(tool)) if tool else None
        key = dumps([VERSION, words, [script_digest(path) for path in sorted(set(scripts))],
                     [os.path.realpath(tool), stat.st_size, stat.st_mtime_ns] if tool else None])
        self.key = sha256_hex(key.encode())
        self.manifest = os.path.join(opts["objcache"], self.key[:2], self.key + ".json")

    def unchanged(self, entry):
        """Every dependency still has the content the entry recorded. An entry stores each file's (size, mtime) beside
        its hash: a file that still has that signature is the same file (one stat, the digest memo is not even read).
        A file with another signature (a fresh build tree's copy of the shim, a worktree checked out later) is hashed
        where it stands when it is small, which is cheaper than parsing the megabytes of the memo; only a big file goes
        through the memo. An entry from before signatures is rewritten with them (restore())."""
        sigs = entry.get("sigs") or []
        for index, (path, value) in enumerate(entry["deps"]):
            path = unmapped(path, self.maps)
            try:
                stat = os.stat(path)
                if index < len(sigs) and sigs[index] == [stat.st_size, stat.st_mtime_ns]:
                    continue
                now = script_digest(path) if stat.st_size <= SMALL else self.digests.of(path)
                if now != value:
                    return False
            except OSError:
                return False
        return True

    def restore(self):
        try:
            entries, current = load_manifest(self.manifest)
        except (OSError, ValueError):
            return False
        for entry in entries:
            if not self.unchanged(entry):
                continue
            if not current or not entry.get("sigs"):
                try:
                    self.sign(entry)
                    self.write(entries)
                except OSError:
                    pass
            outs = self.opts["outs"]
            files = list(entry["files"])
            for index, (path, text) in enumerate(zip(outs, files, strict=False)):
                if path.endswith(".gcno") and isinstance(text, bytes):
                    files[index] = gcno_cwd(text, os.getcwd())
                    if files[index] is None:  # a layout this cannot rewrite: build it, never restore a wrong one
                        return False
            for path, text in zip(outs, files):
                if text is None:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if not isinstance(text, bytes):  # JSON manifests of before hold base64
                        import binascii
                        text = binascii.a2b_base64(text)
                    with open(path, "wb") as stream:
                        stream.write(text)
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

    def sign(self, entry):
        entry["sigs"] = [[stat.st_size, stat.st_mtime_ns]
                         for stat in (os.stat(unmapped(path, self.maps)) for path, _ in entry["deps"])]

    def write(self, entries):
        try:
            os.makedirs(os.path.dirname(self.manifest), exist_ok=True)
            temporary = f"{self.manifest}.{os.getpid()}.tmp"
            with open(temporary, "wb") as stream:
                stream.write(MAGIC + marshal.dumps(entries))
            os.replace(temporary, self.manifest)
        except OSError:
            pass  # a full or read-only cache only costs the speed

    def store(self):
        depfile = self.opts.get("depfile")
        if not depfile or not os.path.exists(depfile):
            return
        try:
            paths = dependencies(depfile)
            # signatures first: a file edited while it is hashed then fails the stat check instead of passing it
            sigs = [[stat.st_size, stat.st_mtime_ns] for stat in map(os.stat, paths)]
            deps = [[mapped(path, self.maps), self.digests.of(path)] for path in paths]
            files = []
            for path in self.opts["outs"]:
                if os.path.exists(path):
                    with open(path, "rb") as stream:
                        files.append(stream.read())
                else:
                    files.append(None)
            try:
                entries = load_manifest(self.manifest)[0]
            except (OSError, ValueError):
                entries = []
            entry = {"deps": deps, "sigs": sigs, "files": files}
            self.write([entry] + [e for e in entries if e["deps"] != deps][:3])
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


def gcno_cwd(data, cwd):
    """A GCC 12+ .gcno (magic, version, stamp, checksum, then the compile directory as a byte-length string with its
    NUL) naming CWD as its compile directory; None when DATA is not of that layout."""
    if len(data) < 20 or data[:4] != b"oncg":
        return None
    size = int.from_bytes(data[16:20], "little")
    end = 20 + size
    if size < 2 or end > len(data) or data[end - 1] != 0 or data[20:21] != b"/":
        return None
    name = cwd.encode() + b"\0"
    return data[:16] + len(name).to_bytes(4, "little") + name + data[end:]


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
