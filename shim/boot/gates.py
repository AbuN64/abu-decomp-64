# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-ABI gates over every original unit, before any boot.

The N64 original runs on big-endian MIPS o32; the port runs the same C natively on little-endian i386. Source
that was only ever compiled for MIPS can lean on facts the host does not share, and the failure is silent or
far from its cause (a hang, a wrong value, a fault in a later function). This tool finds the classes statically,
over all prepared units at once (boot units flagged), and writes one needs.jsonl row per site:

  gate-return-type    -Wreturn-type: a non-void body falls off its end (MIPS returns the last call's v0, the host
                      returns garbage in eax or leaves the x87 stack unbalanced)
  gate-implicit-decl  -Wimplicit-function-declaration: a call with no declaration in scope (int return, default
                      promotions: wrong for float, u64 and struct returns or parameters)
  gate-uninit         -Wuninitialized / -Wmaybe-uninitialized: a local read before any write
  gate-knr-args       a call through a prototype-less `()` declaration with fewer arguments than the definition
                      (on MIPS a0-a3 still hold the caller's values, natively the slots are garbage)
  gate-lto-type       conflicting extern declarations across units (-flto -Wlto-type-mismatch over every unit: a
                      caller off the boot path still runs once the game is played)
  gate-u64-param      a long long / u64 parameter in non-SDK code (o32 passes it in an aligned register pair, i386
                      on the stack; a `()` declaration or hand-split halves breaks)
  gate-subword        a data symbol the C reads as a 1/2-byte view inside a 4-byte scalar another symbol names
                      (the byte at +2 of a big-endian u32 is its low half, natively the high half)
  gate-layout         sizeof/offsetof/alignof of a struct/union the boot units use differs between
                      mips-linux-gnu-gcc -mabi=32 -EB and gcc -m32 (the missing -malign-double class)
  gate-layout-bitfield  a struct with bit-fields (bit order differs by endianness by construction; listed)
  gate-struct-conflict  one tag or typedef name defined with two layouts across boot units
  gate-prepare-failed   a unit that does not preprocess at all

Rows use the needs.py fields (symbol, class, unit, file_line, first_error, pin, trace_order, halt) plus `boot`.
`--baseline FILE --check` fails on a site the baseline lacks (key: class, unit, symbol; no line numbers), so CI
refuses a new site (a class whose count rises above the baseline's) while the old ones are worked off class by class
(`--update-baseline` rewrites the file; lower it as batches land).
The runtime half is the RAGE_DECOMP_UBSAN flavour in cmake/decomp.cmake (diagnostic, never shipped).
"""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import portdir  # noqa: E402
import srcfiles  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True

# The flags of cmake/decomp.cmake (test_gates checks they stay in step).
HOST_FLAGS = ["-m32", "-std=gnu89", "-fno-strict-aliasing", "-fwrapv", "-msse2", "-mfpmath=sse"]
MIPS_FLAGS = ["-mabi=32", "-EB"]
WARNINGS = ["-Wreturn-type", "-Wimplicit-function-declaration", "-Wuninitialized", "-Wmaybe-uninitialized"]
CLASS_OF_WARNING = {"return-type": "gate-return-type", "implicit-function-declaration": "gate-implicit-decl",
                    "uninitialized": "gate-uninit", "maybe-uninitialized": "gate-uninit"}
INFORMATIONAL = {"gate-knr-extra"}
TIMEOUT = 180
SCAN_VERSION = 3  # bump when scan_one's result shape or the checks it runs change (invalidates the cache)
PROBE_VERSION = 1  # bump when probe_source/probe_unit change what a struct's probe measures


# ---------------------------------------------------------------- units

def unit_list(source):
    """Every original unit, as the cmake glob sees it (libgcc excluded), sorted src-relative paths."""
    source = pathlib.Path(source)
    return sorted(str(p.relative_to(source)) for p in srcfiles.sources(source))


def load_policy(adapter=HERE):
    sys.path.insert(0, str(adapter))
    try:
        import policy
        return policy
    finally:
        sys.path.pop(0)


def boot_units(source, manifest, adapter=HERE):
    """The units the boot executable links: the manifest's sanitized closure plus the resident data image.
    A name the manifest holds but the tree lacks (the source moved on past the pin) is dropped first."""
    source, manifest = pathlib.Path(source), pathlib.Path(manifest)
    project = json.loads(manifest.read_text())
    project["sources"] = [u for u in project.get("sources", []) if (source / u).exists()]
    with tempfile.TemporaryDirectory() as tmp:
        filtered = pathlib.Path(tmp) / manifest.name
        filtered.write_text(json.dumps(project))
        project = load_policy(adapter).load_project(filtered, source)
        units = set(project.get("sources", []))
        image = subprocess.run([sys.executable, "-I", str(adapter / "image.py"), "select", "--source", str(source),
                                "--manifest", str(filtered)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    if image.returncode == 0:
        units |= {u for u in re.split(r"[;\s]+", image.stdout.strip()) if u}
    return {u for u in units if (source / u).exists()}


def preprocess_flags(source, adapter=HERE):
    source = pathlib.Path(source)
    return ["-DNON_MATCHING", "-DVERSION_US_REV1", "-D_LANGUAGE_C", "-DF3DEX_GBI_2", "-include",
            str(pathlib.Path(adapter) / "native.h"), "-I" + str(source / "include"),
            "-I" + str(source / "src" / "sdk" / "libgcc")]


def run(command, timeout=TIMEOUT):
    """(returncode, stdout, stderr); a timeout is returncode -9."""
    try:
        done = subprocess.run(["nice", "-n", "10"] + command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors="replace", timeout=timeout,
                              env=dict(os.environ, LC_ALL="C"))
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return -9, "", "timeout after %ds" % timeout


def read_depfile(path):
    """The prerequisites of a make-format depfile (the source and every header the unit read)."""
    text = pathlib.Path(path).read_text(errors="replace").replace("\\\n", " ")
    return sorted(set(text.split(":", 1)[-1].split()))
_STATE = {}
def file_state(path):
    """[size, mtime_ns, sha1] of a file, memoised per process; [] when it is gone."""
    if path not in _STATE:
        try:
            st = os.stat(path)
            _STATE[path] = [st.st_size, st.st_mtime_ns, hashlib.sha1(pathlib.Path(path).read_bytes()).hexdigest()]
        except OSError:
            _STATE[path] = []
    return _STATE[path]
def deps_unchanged(recorded):
    """True when every recorded {path: [size, mtime_ns, sha1]} still holds (the stat pair is the fast path, the
    content hash the fallback, so a touched file with the same bytes stays valid)."""
    for path, old in recorded.items():
        try:
            st = os.stat(path)
        except OSError:
            return False
        if [st.st_size, st.st_mtime_ns] == old[:2]:
            continue
        if file_state(path)[2:] != old[2:]:
            return False
    return True
def unit_fingerprint(ctx, unit):
    """What a cached scan also depends on besides the files it read: flags, the checks, the unit's class."""
    # Layout adds DWARF structs to boot units only, so a --no-layout seed slice warms every other unit.
    prep = pathlib.Path(ctx["adapter"]) / "prepare.py"
    return hashlib.sha1(json.dumps([SCAN_VERSION, HOST_FLAGS, WARNINGS, preprocess_flags(ctx["source"], ctx["adapter"]),
                                    bool(ctx["layout"]) and unit in ctx["boot"], unit in ctx["boot"], unit in ctx["sdk"], bool(ctx.get("build")),
                                    file_state(str(prep))[2:]]).encode()).hexdigest()
def cache_file(ctx, unit):
    return pathlib.Path(ctx["cache"]) / "u" / (unit + ".json")
def cache_load(ctx, unit):
    """The cached scan result of a unit whose files, flags and prepared .i are all unchanged, else None."""
    try:
        entry = json.loads(cache_file(ctx, unit).read_text())
        if entry["fp"] != unit_fingerprint(ctx, unit) or not deps_unchanged(entry["deps"]):
            return None
        if not (pathlib.Path(ctx["work"]) / "i" / (unit + ".i")).exists() and not ctx.get("build"):
            return None
        result = entry["result"]
        result["warnings"] = [tuple(w) for w in result["warnings"]]
        result["cached"] = True
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None
def cache_store(ctx, unit, deps, result):
    state = {d: file_state(d) for d in deps}
    if not all(state.values()):
        return
    target = cache_file(ctx, unit)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps({"fp": unit_fingerprint(ctx, unit), "deps": state, "result": result}))
    tmp.replace(target)
def prepare_unit(unit, source, work, adapter=HERE, build=None, deps=None, exact=True):
    """The unit's prepared translation unit (prepare.py's output, or the fl build's own .i when it holds one);
    returns (path, mode) or (None, error). exact=False is the cheap path: prepare.py's own `gcc -E` and nothing else
    (no syntax pre-check, marker scan or stubs, which cost ~4x the preprocess); it is the same .i unless the
    NON_MATCHING C does not compile, which scan_one detects from the warning compile and redoes with exact=True."""
    adapter = pathlib.Path(adapter)
    if build:
        ready = pathlib.Path(build) / "decomp" / (unit + ".i")
        inv = pathlib.Path(build) / "decomp" / (unit + ".json")
        if ready.exists() and inv.exists():
            try:
                mode = json.loads(inv.read_text()).get("mode", "NON_MATCHING")
            except ValueError:  # an interrupted prepare left an empty manifest: prepare the unit here
                mode = None
            if mode is not None:
                if deps is not None:
                    deps += [str(ready), str(inv)]
                return ready, mode
    out = pathlib.Path(work) / "i" / (unit + ".i")
    stubs, inv, dep = (out.with_name(out.name + s) for s in (".stubs.c", ".json", ".d"))
    out.parent.mkdir(parents=True, exist_ok=True)
    if not exact:
        dep = out.with_name(out.name + ".d")
        code, text, _ = run(["gcc"] + HOST_FLAGS + preprocess_flags(source, adapter)
                            + ["-x", "c", "-E", "-MD", "-MF", str(dep), "-MT", str(out), str(pathlib.Path(source) / unit)])
        if not code:
            out.write_text(text)
            if deps is not None and dep.exists():
                deps += read_depfile(dep)
            dep.unlink(missing_ok=True)
            return out, "NON_MATCHING"
        dep.unlink(missing_ok=True)
    code, _, err = run([sys.executable, "-I", str(adapter / "prepare.py"), "--compiler", "gcc", "--source",
                        str(pathlib.Path(source) / unit), "--output", str(out), "--stubs", str(stubs),
                        "--inventory", str(inv), "--depfile", str(dep), "--"] + HOST_FLAGS
                       + preprocess_flags(source, adapter))
    mode = "NON_MATCHING"
    if not code and inv.exists():
        mode = json.loads(inv.read_text()).get("mode", mode)
    if deps is not None and dep.exists():
        deps += read_depfile(dep)
    for scratch in (stubs, dep, inv):
        scratch.unlink(missing_ok=True)
    if code:
        return None, err.strip().splitlines()[-1] if err.strip() else "prepare failed"
    return out, mode


# ---------------------------------------------------------------- compiler warnings

WARN = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):\d+: warning: (?P<msg>.*) \[-W(?P<flag>[\w-]+)\]$")
INFUNC = re.compile(r"^(?P<file>[^:\s][^:]*): In function '(?P<fn>[^']+)':$")
QUOTED = re.compile(r"'([^']+)'")


def relative(path, source):
    path = str(path)
    root = str(source).rstrip("/") + "/"
    return path[len(root):] if path.startswith(root) else path


def parse_warnings(text, source):
    """gcc stderr -> [(class, symbol, file, line, message)] for the warning classes in CLASS_OF_WARNING."""
    rows, function = [], ""
    for line in text.splitlines():
        m = INFUNC.match(line)
        if m:
            function = m["fn"]
            continue
        m = WARN.match(line)
        if not m or m["flag"] not in CLASS_OF_WARNING:
            continue
        cls = CLASS_OF_WARNING[m["flag"]]
        names = QUOTED.findall(m["msg"])
        symbol = names[0] if cls == "gate-implicit-decl" and names else function or (names[0] if names else "")
        rows.append((cls, symbol, relative(m["file"], source), int(m["line"]), m["msg"]))
    return rows


# ---------------------------------------------------------------- the translation-unit reader

MARKER = re.compile(r'^#\s*(\d+)\s+"([^"]*)"')
LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
ATTRIBUTE = re.compile(r"__attribute__\s*\(\(")
LL = re.compile(r"\blong\s+long\b")
KEYWORDS = {"return", "else", "do", "case", "sizeof", "goto", "typeof", "const", "volatile", "static", "extern"}


def strip_attributes(text):
    """Remove __attribute__((...)) with balanced parentheses."""
    while True:
        m = ATTRIBUTE.search(text)
        if not m:
            return text
        depth, i = 2, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        text = text[:m.start()] + " " + text[i:]


def top_level(text):
    """Split preprocessed C into top-level items: (head, body, file, line). A function definition has its body
    text; every other item (declaration, typedef, struct definition, initializer) has body None and the full
    text (nested braces kept) in head. Line numbers are the source's own (through the # linemarkers)."""
    file, line = "", 1
    depth, head, body, start = 0, [], [], None
    for raw in text.split("\n"):
        m = MARKER.match(raw)
        if m:
            file, line = m.group(2), int(m.group(1))
            continue
        row_text = LITERAL.sub('""', raw)
        if start is None and row_text.strip() and depth == 0:
            start = (file, line)
        for ch in row_text:
            if depth == 0:
                if ch == ";":
                    start = start or (file, line)  # a stray `;` after a body or another `;`
                    yield "".join(head), None, start[0], start[1]
                    head, start = [], None
                elif ch == "{":
                    depth, body = 1, []
                else:
                    head.append(ch)
            else:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                if depth == 0:
                    start = start or (file, line)
                    text_head = strip_attributes("".join(head)).strip()
                    if text_head.endswith(")") and "=" not in text_head and not re.match(
                            r"(typedef|struct|union|enum)\b", text_head):
                        yield text_head, "".join(body), start[0], start[1]
                        head, start = [], None
                    else:
                        head.append("{" + "".join(body) + "}")
                else:
                    body.append(ch)
        (body if depth else head).append("\n")
        line += 1


def split_commas(text):
    depth, cur, out = 0, [], []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def declarator(head):
    """(name, params-text) of a function declarator `... name(params)` at the end of head, or None."""
    head = strip_attributes(head).strip()
    if not head.endswith(")"):
        return None
    depth = 0
    for i in range(len(head) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(head[i], 0)
        if depth == 0:
            before = head[:i].rstrip()
            m = re.search(r"(\w+)$", before)
            if not m or before.endswith(")"):
                return None
            return m.group(1), head[i + 1:-1]
    return None


def count_params(params):
    params = params.strip()
    if params in ("", "void"):
        return 0
    return -1 if "..." in params else len(split_commas(params))


def scan_text(text, unit_is_sdk=False):
    """Facts one prepared unit shows: function definitions, `()` declarations, long long parameters and the
    bodies (for call analysis). Pure text analysis, no compiler."""
    ll_types = set()
    defs, knr_decls, bodies, u64 = {}, set(), [], []
    for head, body, file, line in top_level(text):
        h = strip_attributes(head).strip()
        if body is None:
            td = re.match(r"typedef\s+(.*?)(\w+)\s*$", h, re.S)
            if td:
                kind = td.group(1)
                words = kind.split()
                if "(" not in kind and "*" not in kind and "[" not in h and "{" not in h and (
                        LL.search(kind) or (words and words[-1] in ll_types)):
                    ll_types.add(td.group(2))
                continue
            d = declarator(h)
            if d and d[1].strip() == "" and "=" not in h and "(*" not in re.sub(r"\s+", "", h):
                knr_decls.add(d[0])
            continue
        d = declarator(h)
        if not d:
            continue
        name, params = d
        defs[name] = (count_params(params), bool(re.search(r"\bstatic\b", h)), file, line)
        bodies.append((name, body, file, line))
        if unit_is_sdk or params.strip() in ("", "void"):
            continue
        for index, param in enumerate(split_commas(params)):
            p = param.strip()
            if "*" in p or "[" in p or "(" in p:
                continue
            words = re.findall(r"[A-Za-z_]\w*", p)
            if LL.search(p) or any(w in ll_types for w in words[:-1]):
                u64.append((name, index, p, file, line))
    return {"defs": defs, "knr_decls": sorted(knr_decls), "bodies": bodies, "u64": u64}


def split_args(text):
    depth, cur, out = 0, [], []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def calls_of(scan, names):
    """[(callee, nargs, caller, file, line)] for calls inside the unit's bodies to any of `names`."""
    found = []
    if not names:
        return found
    pattern = re.compile(r"(?<![\w.>])(" + "|".join(map(re.escape, sorted(names))) + r")\s*\(")
    for caller, body, file, line in scan["bodies"]:
        for m in pattern.finditer(body):
            before = body[:m.start()].rstrip()
            args = split_args(body[m.end():])
            word = re.search(r"(\w+)$", before)
            if not args and (before.endswith("*") or (word and word.group(1) not in KEYWORDS)):
                continue  # a local `type f();` declaration, not a call
            found.append((m.group(1), len(args), caller, file, line + body.count("\n", 0, m.start())))
    return found


def knr_rows(scans, source, pin, boot):
    """Join every unit's `()` calls with the definitions: fewer arguments than parameters is a row (extra
    arguments are harmless on cdecl and only counted as gate-knr-extra). scans: unit -> {defs, calls}."""
    extern_defs, local_defs = collections.defaultdict(set), {}
    for unit, scan in scans.items():
        for name, (n, static, file, _line) in scan["defs"].items():
            if static:
                local_defs[(unit, name)] = n
            else:
                extern_defs[name].add((n, relative(file, source)))
    rows = []
    for unit, scan in sorted(scans.items()):
        for callee, nargs, caller, file, line in scan["calls"]:
            if (unit, callee) in local_defs:
                n, where = local_defs[(unit, callee)], unit
            else:
                options = extern_defs.get(callee, set())
                if len({n for n, _ in options}) != 1:
                    continue
                n, where = sorted(options)[0]
            if n < 0 or n == nargs:
                continue
            cls = "gate-knr-args" if nargs < n else "gate-knr-extra"
            rows.append(row(cls, callee, unit, "%s:%d" % (relative(file, source), line), pin, unit in boot,
                            "%s calls %s with %d argument(s), the definition in %s takes %d" % (
                                caller, callee, nargs, where, n)))
    return rows


# ---------------------------------------------------------------- struct layout

def die_tree(dump):
    """readelf --debug-dump=info -> {offset: {tag, attrs, children, depth}}."""
    dies, stack, last = {}, [], None
    head = re.compile(r"^\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: \d+ \((\w+)\)")
    attr = re.compile(r"^\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)$")
    for line in dump.splitlines():
        m = head.match(line)
        if m:
            depth, off = int(m.group(1)), int(m.group(2), 16)
            last = {"tag": m.group(3), "attrs": {}, "children": [], "depth": depth}
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if stack:
                dies[stack[-1][1]]["children"].append(off)
            stack.append((depth, off))
            dies[off] = last
            continue
        m = attr.match(line)
        if m and last is not None:
            value = m.group(2).strip()
            if m.group(1) == "DW_AT_name":
                value = value.split("): ", 1)[1] if "): " in value else value
            last["attrs"][m.group(1)] = value
    return dies


def ref(value):
    m = re.search(r"<0x([0-9a-f]+)>", value or "")
    return int(m.group(1), 16) if m else None


def number(value):
    m = re.match(r"(-?\d+|0x[0-9a-f]+)", value or "")
    return int(m.group(1), 0) if m else None


def is_wide(dies, type_ref):
    """The member's type is an 8-byte scalar by value (long long, double, u64/f64 typedefs of them)."""
    for _ in range(12):
        die = dies.get(type_ref)
        if die is None:
            return False
        if die["tag"] == "DW_TAG_base_type":
            return number(die["attrs"].get("DW_AT_byte_size")) == 8
        if die["tag"] not in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type", "DW_TAG_array_type"):
            return False
        type_ref = ref(die["attrs"].get("DW_AT_type"))
    return False


def anonymous_members(dies, die, depth=0):
    """Members of the anonymous structs/unions a type holds (inline definitions, or members whose type is an
    unnamed struct/union through typedefs and arrays), at any depth."""
    out = []
    for child in die["children"]:
        member = dies[child]
        if member["tag"] != "DW_TAG_member" or depth > 8:
            continue
        target = ref(member["attrs"].get("DW_AT_type"))
        for _ in range(12):
            inner = dies.get(target)
            if inner is None or inner["tag"] not in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
                                                     "DW_TAG_array_type"):
                break
            target = ref(inner["attrs"].get("DW_AT_type"))
        inner = dies.get(target)
        if inner and inner["tag"] in ("DW_TAG_structure_type", "DW_TAG_union_type") and not inner["attrs"].get(
                "DW_AT_name"):
            out += [m for c in inner["children"] for m in [dies[c]] if m["tag"] == "DW_TAG_member"]
            out += anonymous_members(dies, inner, depth + 1)
    return out


def dwarf_structs(dump):
    """Top-level complete struct/union types of one object's DWARF:
    [{expr, size, members: [{name, bit, wide}], wide, bitfields}] (expr is what C writes: `struct tag`,
    `union tag`, or the typedef name of an anonymous type)."""
    dies = die_tree(dump)
    typedef_of = {}
    for die in dies.values():
        if die["tag"] == "DW_TAG_typedef" and die["depth"] == 1:
            target = ref(die["attrs"].get("DW_AT_type"))
            if target is not None:
                typedef_of.setdefault(target, die["attrs"].get("DW_AT_name"))
    out = []
    for off, die in sorted(dies.items()):
        if die["tag"] not in ("DW_TAG_structure_type", "DW_TAG_union_type") or die["depth"] != 1:
            continue
        size = number(die["attrs"].get("DW_AT_byte_size"))
        if size is None or "DW_AT_declaration" in die["attrs"]:
            continue
        kind = "struct" if die["tag"] == "DW_TAG_structure_type" else "union"
        name = die["attrs"].get("DW_AT_name")
        expr = "%s %s" % (kind, name) if name else typedef_of.get(off)
        if not expr:
            continue
        members, wide, bitfields = [], 0, 0
        for inner in anonymous_members(dies, die):
            bitfields += "DW_AT_bit_size" in inner["attrs"]
            wide += is_wide(dies, ref(inner["attrs"].get("DW_AT_type")))
        for child in die["children"]:
            member = dies[child]
            if member["tag"] != "DW_TAG_member":
                continue
            bit = "DW_AT_bit_size" in member["attrs"]
            w = is_wide(dies, ref(member["attrs"].get("DW_AT_type")))
            bitfields += bit
            wide += w
            if member["attrs"].get("DW_AT_name"):
                members.append({"name": member["attrs"]["DW_AT_name"], "bit": bit, "wide": w})
        out.append({"expr": expr, "size": size, "members": members, "wide": wide, "bitfields": bitfields,
                    "shape": hashlib.sha1(json.dumps(type_shape(dies, off)).encode()).hexdigest()})
    return out


def type_shape(dies, off, depth=0):
    """Everything about a type that its probe can measure, through typedefs, qualifiers, arrays and nested
    structs/unions (pointers stop the walk: their layout does not depend on the target), so a probe result is
    reused for a struct whose type closure is unchanged whichever unit or header edit surrounds it.
    Not seen: an aligned()/packed attribute that moves no host offset or size (DWARF 4 has no DW_AT_alignment)."""
    die = dies.get(off)
    if die is None or depth > 16:
        return None
    a, tag = die["attrs"], die["tag"]
    if tag in ("DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type"):
        return type_shape(dies, ref(a.get("DW_AT_type")), depth + 1)
    if tag == "DW_TAG_base_type":
        return ["b", a.get("DW_AT_name"), number(a.get("DW_AT_byte_size")), a.get("DW_AT_encoding")]
    if tag in ("DW_TAG_pointer_type", "DW_TAG_subroutine_type"):
        return ["p", number(a.get("DW_AT_byte_size"))]
    if tag == "DW_TAG_enumeration_type":
        return ["e", number(a.get("DW_AT_byte_size"))]
    if tag == "DW_TAG_array_type":
        dims = [[dies[c]["attrs"].get("DW_AT_upper_bound"), dies[c]["attrs"].get("DW_AT_count")]
                for c in die["children"] if dies[c]["tag"] == "DW_TAG_subrange_type"]
        return ["a", dims, type_shape(dies, ref(a.get("DW_AT_type")), depth + 1)]
    if tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
        members = [[m["attrs"].get(k) for k in ("DW_AT_name", "DW_AT_data_member_location", "DW_AT_bit_size",
                                               "DW_AT_data_bit_offset", "DW_AT_bit_offset")]
                   + [type_shape(dies, ref(m["attrs"].get("DW_AT_type")), depth + 1)]
                   for c in die["children"] for m in [dies[c]] if m["tag"] == "DW_TAG_member"]
        return [tag[7], number(a.get("DW_AT_byte_size")), "DW_AT_declaration" in a, members]
    return [tag]


def signature(struct):
    return json.dumps([struct["expr"], struct["size"], [m["name"] for m in struct["members"]]])


def probe_source(structs):
    """C appended to a prepared unit: one `unsigned __gpI_J` per fact (value + 1, so nothing is zero-filled):
    sizeof, alignof, then the offset of every named non-bit-field member."""
    lines = ["\n"]
    for i, s in enumerate(structs):
        facts = ["sizeof(%s)" % s["expr"], "__alignof__(%s)" % s["expr"]]
        facts += ["__builtin_offsetof(%s, %s)" % (s["expr"], m["name"]) for m in s["members"] if not m["bit"]]
        for j, fact in enumerate(facts):
            lines.append("unsigned __gp%d_%d = (unsigned)(%s) + 1;\n" % (i, j, fact))
    return "".join(lines)


def probe_values(asm):
    """Assembly of probe_source -> {(I, J): value}."""
    values, label = {}, None
    for line in asm.splitlines():
        m = re.match(r"^__gp(\d+)_(\d+):", line)
        if m:
            label = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.match(r"^\s*\.(?:long|word|4byte)\s+(-?\d+)", line)
        if m and label:
            values[label] = int(m.group(1)) - 1
            label = None
    return values


def fact_names(struct):
    return ["sizeof", "alignof"] + [m["name"] for m in struct["members"] if not m["bit"]]


def differences(struct, a, b):
    return [(n, a.get(i), b.get(i)) for i, n in enumerate(fact_names(struct)) if a.get(i) != b.get(i)]


def types_only(text):
    """The unit without function bodies (each definition becomes a declaration): the probes need the types only, and
    a body may hold host-only inline code another compiler rejects."""
    return "\n".join(head + ";" for head, body, _, _ in top_level(text)) + "\n"


def probe_unit(ipath, structs, compilers, scratch):
    """Compile ipath + probes with each compiler -> {label: {struct index: {fact index: value}}}. A unit one
    compiler rejects with the whole batch is retried one struct at a time."""
    base = types_only(pathlib.Path(ipath).read_text(errors="replace"))
    results = {}
    for label, argv in compilers.items():
        got, serial = {}, 0
        batches = [list(range(len(structs)))]
        while batches:
            batch = batches.pop()
            src = pathlib.Path(scratch) / ("probe_%s_%d.i" % (label, serial))
            serial += 1
            src.write_text(base + "\n" + probe_source([structs[i] for i in batch]))
            code, out, _ = run(argv + ["-w", "-O0", "-S", "-x", "cpp-output", str(src), "-o", "-"])
            src.unlink(missing_ok=True)
            if code:
                if len(batch) > 1:
                    batches += [[i] for i in batch]
                continue
            for (i, j), v in probe_values(out).items():
                got.setdefault(batch[i], {})[j] = v
        results[label] = got
    return results


# ---------------------------------------------------------------- the sub-word view class

def subword_rows(source, version, pin):
    """Data symbols the C reads as a 1/2-byte view inside a 4-byte scalar another symbol names (the sub-word
    alias class; the same rule is proposed as an `unbake audit` check, lanes/GATES-1/unbake-audit-subword.patch)."""
    source = pathlib.Path(source)
    path = source / "versions" / version / "symbol_addrs.txt"
    if not path.exists():
        return []
    addr = {}
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(\w+)\s*=\s*0x([0-9A-Fa-f]+);", line)
        if m:
            addr[m.group(1)] = int(m.group(2), 16)
    by_addr = {}
    for name, a in addr.items():
        by_addr.setdefault(a, []).append(name)
    wide = {"u32", "s32", "int", "unsigned int", "signed int", "f32", "float", "long", "unsigned long", "OSTime"}
    narrow = {"u16": 2, "s16": 2, "short": 2, "unsigned short": 2, "u8": 1, "s8": 1, "char": 1, "unsigned char": 1,
              "signed char": 1}
    decl = re.compile(r"\bextern\s+(?:volatile\s+)?((?:unsigned |signed )?\w+)\s+(\*?)\s*(D_\w+)\s*;")
    types, users = {}, {}
    for p in srcfiles.sources(source) + srcfiles.headers(source):
        for m in decl.finditer(p.read_text(errors="replace")):
            types.setdefault(m.group(3), set()).add("ptr" if m.group(2) else m.group(1))
            users.setdefault(m.group(3), set()).add(str(p.relative_to(source)))
    rows = []
    for view, vtypes in sorted(types.items()):
        width = max((narrow.get(t, 0) for t in vtypes), default=0)
        a = addr.get(view)
        if not width or a is None or a % 4 == 0:
            continue
        for container in by_addr.get(a & ~3, []):
            ctypes = types.get(container, set())
            if ctypes & (wide | {"ptr"}):
                files = sorted(users[view])
                unit = next((f for f in files if f.startswith("src/")), files[0])
                rows.append(row("gate-subword", view, unit, files[0], pin, None,
                                "%s (%s) views +%d of %s (%s)" % (view, "/".join(sorted(vtypes)), a & 3, container,
                                                                  "/".join(sorted(ctypes)))))
    return rows


# ---------------------------------------------------------------- rows, classes, baseline

def row(cls, symbol, unit, file_line, pin, boot, error):
    return {"symbol": symbol, "class": cls, "unit": unit, "file_line": file_line, "first_error": error,
            "pin": pin, "trace_order": None, "halt": False, "boot": boot}


def key(r):
    return "%s\t%s\t%s" % (r["class"], r["unit"], r["symbol"])


def read_baseline(path):
    path = pathlib.Path(path)
    return set(path.read_text().splitlines()) if path.exists() else set()


def new_sites(rows, baseline):
    """Sites that make CI fail: the keys missing from the baseline, in every class whose site count exceeds the
    baseline's. A rename or move (same count, new key) passes, so a pin bump that follows the source does not trip
    the gate; one more site than the baseline holds does."""
    now = {key(r) for r in rows if r["class"] not in INFORMATIONAL}
    count_now = collections.Counter(k.split("\t")[0] for k in now)
    count_base = collections.Counter(k.split("\t")[0] for k in baseline)
    over = {cls for cls, n in count_now.items() if n > count_base.get(cls, 0)}
    return sorted(k for k in now - set(baseline) if k.split("\t")[0] in over)


def render_classes(rows, meta):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["class"]].append(r)
    lines = ["# GATES-1 classes (%s)" % meta.get("when", ""), "",
             "RW %s, pin %s; %d units swept, %d boot units. One needs.jsonl row per site; folder = first path "
             "component under src/. A subword row carries no boot flag (symbol-level)." % (
                 meta.get("rw", "?"), meta.get("pin", "?"), meta.get("units", 0), meta.get("boot_units", 0)), "",
             "| class | sites | boot sites | boot units | units | folder split (sites) |", "|---|---|---|---|---|---|"]
    for cls in sorted(by):
        rs = by[cls]
        split = collections.Counter(r["unit"].split("/")[1] if r["unit"].startswith("src/") else r["unit"].split("/")[0]
                                    for r in rs)
        lines.append("| %s | %d | %d | %d | %d | %s |" % (
            cls, len(rs), sum(1 for r in rs if r["boot"]), len({r["unit"] for r in rs if r["boot"]}),
            len({r["unit"] for r in rs}), ", ".join("%s %d" % kv for kv in split.most_common())))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the sweep

def scan_one(args):
    """Worker: prepare one unit, compile it with the warning classes, read its text facts and (boot units) its
    struct list. Returns a picklable dict."""
    unit, ctx = args
    source, work = ctx["source"], ctx["work"]
    result = {"unit": unit, "boot": unit in ctx["boot"], "warnings": [], "prepare_error": None,
              "dwarf_error": None, "structs": [], "defs": {}, "calls": [], "u64": []}
    if ctx.get("cache"):
        hit = cache_load(ctx, unit)
        if hit is not None:
            return hit
    deps = [] if ctx.get("cache") else None
    path, mode = prepare_unit(unit, source, work, ctx["adapter"], ctx.get("build"), deps, exact=False)
    if path is not None and not ctx.get("build"):
        code, _, err = run(["gcc"] + HOST_FLAGS + ["-O1"] + WARNINGS + ["-x", "cpp-output", "-c", str(path), "-o", os.devnull])
        if code and "error:" in err:  # the fuzzy NON_MATCHING C does not compile: prepare.py's matched-C fallback
            path, mode = prepare_unit(unit, source, work, ctx["adapter"], None, deps)
            code = None
    else:
        code = None
    if path is None:
        result["prepare_error"] = mode
        if deps:
            cache_store(ctx, unit, deps, result)
        return result
    result["mode"] = mode
    if code is None:
        _, _, err = run(["gcc"] + HOST_FLAGS + ["-O1"] + WARNINGS + ["-x", "cpp-output", "-c", str(path), "-o", os.devnull])
    result["warnings"] = parse_warnings(err, source)
    scan = scan_text(pathlib.Path(path).read_text(errors="replace"), unit in ctx["sdk"])
    result["defs"] = scan["defs"]
    result["calls"] = calls_of(scan, scan["knr_decls"])
    result["u64"] = scan["u64"]
    if result["boot"] and ctx["layout"]:
        obj = pathlib.Path(work) / "o" / (unit + ".o")
        obj.parent.mkdir(parents=True, exist_ok=True)
        code, _, err = run(["gcc"] + HOST_FLAGS + ["-g1", "-gdwarf-4", "-fno-eliminate-unused-debug-types", "-O0",
                                                   "-w", "-x", "cpp-output", "-c", str(path), "-o", str(obj)])
        if code == 0:
            code, dump, _ = run(["readelf", "--debug-dump=info", str(obj)])
            result["structs"] = dwarf_structs(dump)
        else:
            result["dwarf_error"] = (err.strip().splitlines() or [""])[-1]
        obj.unlink(missing_ok=True)
    if ctx.get("cache"):
        cache_store(ctx, unit, deps, result)
    elif not result["boot"] and not ctx.get("build"):
        pathlib.Path(path).unlink(missing_ok=True)  # only boot units are read again (layout probes, LTO)
    return result


def probe_one(args):
    unit, wanted, ctx = args
    scratch = pathlib.Path(ctx["work"]) / "probe" / unit.replace("/", "_")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        ipath = pathlib.Path(ctx["work"]) / "i" / (unit + ".i")
        if ctx.get("build") and (pathlib.Path(ctx["build"]) / "decomp" / (unit + ".i")).exists():
            ipath = pathlib.Path(ctx["build"]) / "decomp" / (unit + ".i")
        compilers = {"host": ["gcc"] + HOST_FLAGS, "aligned": ["gcc"] + HOST_FLAGS + ["-malign-double"],
                     "mips": [ctx["mips_gcc"]] + MIPS_FLAGS}
        return unit, wanted, probe_unit(ipath, wanted, compilers, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def layout_rows(boot_scans, pin, ctx, pool):
    """Probe every distinct boot struct once, in the first unit that defines it, under the three compilers."""
    first, layouts, users = {}, collections.defaultdict(set), collections.defaultdict(set)
    for unit, scan in sorted(boot_scans.items()):
        for s in scan["structs"]:
            sig = signature(s)
            layouts[s["expr"]].add(sig)
            users[sig].add(unit)
            first.setdefault(sig, (unit, s))
    by_unit = collections.defaultdict(list)
    for _sig, (unit, s) in sorted(first.items()):
        by_unit[unit].append(s)
    rows, evidence, unprobed = [], [], 0
    # A struct's probe result is reused while its type closure (dwarf_structs shape), the flags and the compilers
    # are unchanged; which unit defines it and how the rest of that unit's .i changed do not matter.
    store = pathlib.Path(ctx["cache"]) / "probes.json" if ctx.get("cache") else None
    try:
        saved = json.loads(store.read_text()) if store else {}
    except (OSError, ValueError):
        saved = {}
    tools = [file_state(shutil.which(c) or c)[2:] for c in ("gcc", ctx["mips_gcc"])]
    keep = {}
    def pkey(unit, s):
        return hashlib.sha1(json.dumps([PROBE_VERSION, HOST_FLAGS, MIPS_FLAGS, tools, signature(s),
                                        s.get("shape")]).encode()).hexdigest()
    jobs, cached = [], {}
    for unit, structs in sorted(by_unit.items()):
        todo = [s for s in structs if pkey(unit, s) not in saved]
        for s in structs:
            if pkey(unit, s) in saved:
                cached[(unit, signature(s))] = saved[pkey(unit, s)]
        if todo:
            jobs.append((unit, todo, ctx))
    fresh = {}
    for unit, structs, got in pool.map(probe_one, jobs, chunksize=2):
        for i, s in enumerate(structs):
            fresh[(unit, signature(s))] = {k: got[k].get(i, {}) for k in ("host", "aligned", "mips")}
            keep[pkey(unit, s)] = fresh[(unit, signature(s))]
    if store:
        saved.update(keep)
        if not ctx.get("partial"):  # a --units slice sees only its own structs: keep the other slices' probes
            live = {pkey(u, s) for u, ss in by_unit.items() for s in ss}
            saved = {k: v for k, v in saved.items() if k in live}
        store.write_text(json.dumps(saved))
    print("layout: %d structs from cache, %d probed" % (len(cached), len(fresh)), file=sys.stderr, flush=True)
    for unit, structs in sorted(by_unit.items()):
        for s in structs:
            res = fresh.get((unit, signature(s))) or cached[(unit, signature(s))]
            # JSON turns the int keys of a cached result into strings
            host, aligned, mips = ({int(a): b for a, b in res[k].items()} for k in ("host", "aligned", "mips"))
            if not host or not mips:
                unprobed += 1
                continue
            rec = {"expr": s["expr"], "unit": unit, "users": len(users[signature(s)]), "wide": s["wide"],
                   "bitfields": s["bitfields"], "host_size": host.get(0), "mips_size": mips.get(0),
                   "host_vs_mips": differences(s, host, mips), "aligned_vs_mips": differences(s, aligned, mips)}
            evidence.append(rec)
            if s["bitfields"]:
                rows.append(row("gate-layout-bitfield", s["expr"], unit, unit, pin, True,
                                "%d bit-field member(s); size host %s / mips %s; bit order differs by endianness" % (
                                    s["bitfields"], rec["host_size"], rec["mips_size"])))
            if rec["host_vs_mips"]:
                shown = ", ".join("%s host %s mips %s" % d for d in rec["host_vs_mips"][:6])
                rows.append(row("gate-layout", s["expr"], unit, unit, pin, True,
                                "layout differs (%d facts, %d 64-bit members): %s" % (
                                    len(rec["host_vs_mips"]), s["wide"], shown)))
    for expr, sigs in sorted(layouts.items()):
        if len(sigs) > 1:
            unit = first[sorted(sigs)[0]][0]
            rows.append(row("gate-struct-conflict", expr, unit, unit, pin, True,
                            "%d layouts across %d boot units" % (len(sigs), len({u for s in sigs for u in users[s]}))))
    return rows, evidence, unprobed


def decision(evidence, shim_text, unprobed):
    """The -malign-double decision record: counts and the parity diff that decide flag vs per-struct."""
    differ = [e for e in evidence if e["host_vs_mips"]]
    residual = [e for e in differ if e["aligned_vs_mips"]]
    shared = sorted(e["expr"] for e in differ if re.search(r"\b" + re.escape(e["expr"].split()[-1]) + r"\b", shim_text))
    return {"structs_probed": len(evidence), "unprobed": unprobed, "differ_without_flag": len(differ),
            "fixed_by_malign_double": len(differ) - len(residual), "still_differ_with_flag": len(residual),
            "bitfield_structs": sorted(e["expr"] for e in evidence if e["bitfields"]),
            "differ_and_named_in_shim": shared,
            "still_differ_examples": [(e["expr"], e["aligned_vs_mips"][:3]) for e in residual[:10]],
            "recommend": "flag" if differ and not residual and not shared else "per-struct"}


def lto_rows(scans, work, pin, jobs, cache=None, ctx=None):
    """-flto -Wlto-type-mismatch over every prepared unit: conflicting declarations of one symbol across units.
    A cached scan or a --build hit can leave a unit's `.i` off this sweep's disk; prepare it here, and say so when
    a unit still does not reach the link, so a shorter object list never reads as fewer mismatch sites."""
    outdir = pathlib.Path(work) / "lto"
    outdir.mkdir(parents=True, exist_ok=True)
    todo = [(u, str(pathlib.Path(work) / "i" / (u + ".i")), str(outdir / (u.replace("/", "_") + ".o")))
            for u, s in sorted(scans.items()) if not s["prepare_error"]]

    def compile_one(item):
        unit, ipath, obj = item
        if not os.path.exists(ipath) and ctx:
            prepare_unit(unit, ctx["source"], work, ctx["adapter"], None, None, exact=False)
        sha = file_state(ipath)[2:]
        stampf = pathlib.Path(obj + ".sha")
        if cache and os.path.exists(obj) and stampf.exists() and stampf.read_text() == json.dumps(sha):
            return obj  # same prepared unit as the last LTO object: reuse it
        code, _, _ = run(["gcc"] + HOST_FLAGS + ["-flto", "-fno-fat-lto-objects", "-O0", "-w", "-x", "cpp-output",
                                                 "-c", ipath, "-o", obj])
        if code == 0:
            stampf.write_text(json.dumps(sha))
        return obj if code == 0 else None
    with concurrent.futures.ThreadPoolExecutor(jobs) as tp:
        objs = [o for o in tp.map(compile_one, todo) if o]
    listing = outdir / "objects.txt"
    listing.write_text("\n".join(objs) + "\n")
    if len(objs) < len(todo):
        print("lto: %d of %d units reached the link (%d dropped by prepare/compile)" % (
            len(objs), len(todo), len(todo) - len(objs)), file=sys.stderr, flush=True)
    memo = pathlib.Path(cache) / "lto.json" if cache else None
    link_key = hashlib.sha1(json.dumps([[o, file_state(o + ".sha")[2:]] for o in objs]).encode()).hexdigest()
    if memo and memo.exists():
        old = json.loads(memo.read_text())
        if old["key"] == link_key:
            return [tuple(r) for r in old["rows"]], old["code"], old["err"]
    code, _, err = run(["gcc", "-m32", "-flto=%d" % jobs, "-flinker-output=nolto-rel", "-r", "-nostdlib",
                        "-Wlto-type-mismatch", "@" + str(listing), "-o", str(outdir / "linked.o")], timeout=3600)
    rows, lines = [], err.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):\d+: warning: (?P<msg>.*) \[-Wlto-type-mismatch\]$", line)
        if m:
            names = QUOTED.findall(m["msg"])
            notes = [x.split("note: ", 1)[1].strip() for x in lines[i + 1:i + 4] if "note:" in x]
            rows.append((names[0] if names else "", m["file"], int(m["line"]), " ".join([m["msg"]] + notes)))
    if memo:
        memo.write_text(json.dumps({"key": link_key, "rows": rows, "code": code, "err": err}))
    return rows, code, err


def sweep(args):
    source = pathlib.Path(args.source).resolve()
    out = pathlib.Path(args.out).resolve()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    project = json.loads(pathlib.Path(args.manifest).read_text())
    pin, version = project.get("source_pin", "")[:7], project.get("version", "us-rev1")
    units = unit_list(source)
    if args.units:
        wanted = set(pathlib.Path(args.units).read_text().split())
        units = [u for u in units if u in wanted]
    boot = boot_units(source, args.manifest)
    policy = load_policy()
    addresses = {n: int(a, 16) for n, (a, _) in policy.pinned(source, version).items()}
    game = policy.game_code(project)
    sdk = {u for u in units if "/sdk/" in u.lower() or policy.sdk_unit(
        u, (source / u).read_text(errors="replace"), addresses, game)}
    ctx = {"source": str(source), "work": str(work), "adapter": HERE, "boot": boot, "sdk": sdk,
           "build": args.build, "layout": not args.no_layout, "mips_gcc": args.mips_gcc, "partial": bool(args.units),
           "cache": None if args.fresh else str(out / "cache")}
    if args.fresh:
        shutil.rmtree(out / "cache", ignore_errors=True)
    scans, rows, evidence, deci, hits = {}, [], [], None, 0
    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        for i, res in enumerate(pool.map(scan_one, [(u, ctx) for u in units], chunksize=8)):
            scans[res["unit"]] = res
            hits += bool(res.get("cached"))
            if i % 500 == 0:
                print("scan %d/%d" % (i, len(units)), file=sys.stderr, flush=True)
        for unit, res in sorted(scans.items()):
            if res["prepare_error"]:
                rows.append(row("gate-prepare-failed", unit, unit, unit, pin, res["boot"], res["prepare_error"]))
            for cls, symbol, file, line, message in res["warnings"]:
                rows.append(row(cls, symbol, unit, "%s:%d" % (file, line), pin, res["boot"], message))
            for name, index, param, file, line in res["u64"]:
                rows.append(row("gate-u64-param", name, unit, "%s:%d" % (relative(file, source), line), pin,
                                res["boot"], "parameter %d `%s` is a 64-bit integer in non-SDK code" % (index, param)))
        print("scan: %d of %d units from cache, %d rescanned" % (hits, len(units), len(units) - hits),
              file=sys.stderr, flush=True)
        rows += knr_rows({u: {"defs": r["defs"], "calls": r["calls"]} for u, r in scans.items()}, source, pin, boot)
        rows += subword_rows(source, version, pin)
        if not args.no_layout:
            lrows, evidence, unprobed = layout_rows({u: r for u, r in scans.items() if r["boot"]}, pin, ctx, pool)
            rows += lrows
            shim = "\n".join(p.read_text(errors="replace") for p in sorted(HERE.parent.glob("n64c_*.[ch]")))
            deci = decision(evidence, shim, unprobed)
            (out / "layout-evidence.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in evidence))
            (out / "align-double.json").write_text(json.dumps(deci, indent=1, sort_keys=True) + "\n")
    if args.ubsan_units:
        names = set()
        for line in (portdir.fact("boot-trace.tsv")).read_text().splitlines()[1:]:
            names.update(c for c in line.split("\t")[2:4] if c)
        listed = sorted(u for u, r in scans.items() if names & set(r["defs"]))
        pathlib.Path(args.ubsan_units).write_text("".join(u + "\n" for u in listed))
        print("ubsan units: %d of %d define a boot-trace function" % (len(listed), len(scans)), file=sys.stderr)
    if args.lto:
        lrows, code, err = lto_rows(scans, work, pin, args.jobs, ctx["cache"], ctx)
        for symbol, file, line, message in lrows:
            f = relative(file, source)
            rows.append(row("gate-lto-type", symbol, f, "%s:%d" % (f, line), pin, f in boot, message))
        (out / "lto.log").write_text(err)
    rows.sort(key=lambda r: (r["class"], r["unit"], r["symbol"], r["file_line"]))
    (out / "needs.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    meta = {"when": time.strftime("%Y-%m-%d %H:%M"), "pin": pin, "units": len(units),
            "boot_units": len(boot & set(units)), "rw": subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE,
                text=True).stdout.strip()}
    (out / "meta.json").write_text(json.dumps(meta) + "\n")
    if args.classes_md:
        pathlib.Path(args.classes_md).write_text(render_classes(rows, meta))
    if args.fresh and not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    else:
        for scratch in ("o", "probe"):
            shutil.rmtree(work / scratch, ignore_errors=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=pathlib.Path, help="ragewars-decomp tree to sweep")
    ap.add_argument("--manifest", type=pathlib.Path, default=portdir.fact("manifest.json"))
    ap.add_argument("--out", type=pathlib.Path, help="result directory (needs.jsonl, layout evidence, work files)")
    ap.add_argument("--build", help="an fl build directory whose prepared units (decomp/<unit>.i) are read when present")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--mips-gcc", default=shutil.which("mips-linux-gnu-gcc") or "mips-linux-gnu-gcc")
    ap.add_argument("--units", help="a file of src/... paths to sweep instead of every unit")
    ap.add_argument("--no-layout", action="store_true", help="skip the struct layout parity stage")
    ap.add_argument("--lto", action="store_true", help="add the -flto -Wlto-type-mismatch stage over every unit")
    ap.add_argument("--ubsan-units", help="write the units that define a boot-trace function here (RAGE_DECOMP_UBSAN_UNITS)")
    ap.add_argument("--fresh", action="store_true", help="ignore and drop the cache (OUT/cache, OUT/work/i): a cold sweep")
    ap.add_argument("--keep-work", action="store_true", help="keep the prepared units under OUT/work")
    ap.add_argument("--classes-md", help="write the class table (count, boot units, folder split) here")
    ap.add_argument("--baseline", type=pathlib.Path, default=portdir.fact("gates-baseline.tsv"))
    ap.add_argument("--check", action="store_true", help="exit 1 on a site the baseline lacks")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--from-rows", type=pathlib.Path, help="skip the sweep: check/update from an existing needs.jsonl")
    args = ap.parse_args(argv)
    if args.from_rows:
        rows = [json.loads(line) for line in args.from_rows.read_text().splitlines() if line]
    else:
        if not args.source or not args.out:
            ap.error("--source and --out are required")
        rows = sweep(args)
    for cls, n in sorted(collections.Counter(r["class"] for r in rows).items()):
        print("%6d  %s" % (n, cls))
    if args.update_baseline:
        keys = sorted({key(r) for r in rows if r["class"] not in INFORMATIONAL})
        args.baseline.write_text("".join(k + "\n" for k in keys))
        print("baseline: %d sites -> %s" % (len(keys), args.baseline))
    if args.check:
        fresh = new_sites(rows, read_baseline(args.baseline))
        for k in fresh:
            print("NEW SITE\t" + k, file=sys.stderr)
        if fresh:
            print("gates: %d new site(s) not in %s" % (len(fresh), args.baseline), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
