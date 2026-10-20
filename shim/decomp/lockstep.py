# SPDX-License-Identifier: GPL-3.0-or-later
"""Lockstep divergence: the first point where the port's boot behaves differently from the oracle's.

Both sides record the same thing at the first hit of each boot-trace function (port: trace.c with N64C_LOCKSTEP=1;
oracle: cake-taste hosts/n64recomp lockstep.cpp with N64R_LOCKSTEP_DIR): per-4-KiB-page hashes of RDRAM with the
changed pages' bytes, and the arguments and return of the first 8 calls of each function.

    lockstep.py types --build BUILD --trace boot-trace.tsv --out types.tsv
    lockstep.py compare --oracle DIR --port DIR --trace boot-trace.tsv [--symbols symbol_addrs.txt]
                        [--port-exe rage_decomp_boot] [--types types.tsv] [--ignore ignore.tsv] [--oracle2 DIR]
                        [--rom baserom.z64] [--json out.json]

compare lists every persistent memory, argument, and return divergence, in order within each category.
The reviewed --accept file marks exact known differences without hiding them; first_after_accepted and
matched_through_after_accepted describe the remaining differences. Raw first/matched_through remain unchanged.
compare prints the first diverging (function, page, symbol+offset, words) and the matched-through number: the largest
first-hit order up to which neither memory nor any recorded call diverges persistently. The oracle keeps canonical
big-endian bytes and the port native ones, so a word matches when the port's bytes are the oracle's as bytes, as
16-bit halves, as one 32-bit value, as half of a 64-bit value, or as the same function or data object (native address
vs original: the port keeps game statics in its executable, so a pointer to native D_80123456+8 is 0x8012345e).
Words that already differ at the first trace function (loader vs shim setup) count only once the game writes them.
Typed call fields that differ between both references are listed as volatile_calls, not persistent differences.
Calls of functions without a known prototype (--types) are not compared: their arity is unknown. Guest stack pages
(from the oracle's stack pointers), words that differ between two oracle runs (--oracle2), ignore.tsv rows (SYMBOL or
0xSTART-0xEND, then a reason) and, with --rom, oracle words still holding IPL3's boot copy of the ROM (the port runs
that code natively and never copies it) are masked. A memory divergence is persistent when the word
still differs at the next --persist compared points. Words holding a host pointer (outside RDRAM, not a function)
are shim-owned state, and floats within 4 ulp are rounding (MIPS single vs x87): listed, never counted. Exit 0 when nothing diverges, 1 when something does, 2 on bad input.
"""
import argparse
import array
import bisect
import concurrent.futures
import csv
import json
import os
import pathlib
import re
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
PAGE = 4096
WORDS = PAGE // 4
RDRAM = 0x80000000
FNV_OFFSET, FNV_PRIME, MASK64 = 0xCBF29CE484222325, 0x100000001B3, (1 << 64) - 1
ORACLE_STACK, PORT_STACK = 0x10000, 0x100000


def page_hash(data):
    """FNV-1a over little-endian 64-bit words, as trace.c and lockstep.cpp compute it."""
    h = FNV_OFFSET
    for (w,) in struct.iter_unpack("<Q", data):
        h = ((h ^ w) * FNV_PRIME) & MASK64
    return h


# ---- inputs -----------------------------------------------------------------------------------------------------
def read_trace(path):
    """order -> names (oracle symbol first) and name -> order, from boot-trace.tsv."""
    names, order_of = {}, {}
    for number, line in enumerate(pathlib.Path(path).read_text().splitlines()):
        fields = line.split("\t")
        if number == 0 or len(fields) < 3 or not fields[0].isdigit():
            continue
        order = int(fields[0])
        names[order] = [n for n in fields[2:] if n]
        for n in names[order]:
            order_of.setdefault(n, order)
    if not names:
        raise ValueError(f"{path}: empty trace")
    return names, order_of


def read_symbols(path):
    """Sorted (address, name) from symbol_addrs.txt, and name -> address."""
    rows, by_name = [], {}
    if path:
        for line in pathlib.Path(path).read_text().splitlines():
            m = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", line)
            if m:
                rows.append((int(m[2], 16), m[1]))
                by_name.setdefault(m[1], int(m[2], 16))
    rows.sort()
    return rows, by_name


def read_exe_symbols(path, kinds):
    """Sorted (address, size, name, local) of the port executable's symbols of the nm kinds given."""
    if not path:
        return []
    out = subprocess.run(["nm", "-S", "-n", "--defined-only", str(path)], capture_output=True, text=True, check=True)
    rows = []
    for line in out.stdout.splitlines():
        f = line.split()
        if len(f) == 4 and f[2] in kinds:
            rows.append((int(f[0], 16), int(f[1], 16), f[3], f[2].islower()))
        elif len(f) == 3 and f[1] in kinds and f[1] in "Tt":
            # Assembly/linker function aliases may have no ELF size. Their
            # exact entry address is still authoritative code identity.
            rows.append((int(f[0], 16), 0, f[2], f[1].islower()))
    return rows


def read_exe_functions(path):
    return read_exe_symbols(path, "Tt")


def read_exe_data(path):
    return [r for r in read_exe_symbols(path, "DdBbRr") if r[1]]


BOOT_COPY = (0x80000400, 0x1000, 0x100000)  # IPL3 copies ROM 0x1000.. (1 MiB) to 0x80000400


def read_boot_copy(path):
    """IPL3's boot copy as canonical big-endian bytes, from a z64/v64/n64 ROM, or None."""
    if not path:
        return None
    rom = bytearray(pathlib.Path(path).read_bytes()[:BOOT_COPY[1] + BOOT_COPY[2]])
    magic = bytes(rom[:4])
    if magic == bytes.fromhex("37804012"):
        rom[0::2], rom[1::2] = rom[1::2], rom[0::2]
    elif magic == bytes.fromhex("40123780"):
        words = array.array("I", bytes(rom[:len(rom) // 4 * 4]))
        words.byteswap()
        rom = bytearray(words.tobytes())
    elif magic != bytes.fromhex("80371240"):
        raise ValueError(f"{path}: not an N64 ROM")
    return bytes(rom[BOOT_COPY[1]:])


class Side:
    """One run's lockstep files: points (key -> page-hash array), calls and returns (key -> index -> record)."""

    def __init__(self, directory, key_of):
        self.dir = pathlib.Path(directory)
        self.points, self.calls, self.rets, self.sps, self.watch = {}, {}, {}, {}, []
        self.meta, self.store, self.offsets = None, None, {}
        current = None
        for number, line in enumerate((self.dir / "lockstep.jsonl").read_text().splitlines()):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                if number:  # the last line of a run killed mid-write
                    continue
                raise ValueError(f"{self.dir}: unreadable meta line")
            kind = r.get("k")
            if kind == "meta":
                self.meta = r
                current = array.array("Q", [0]) * (r["ram"] // PAGE)
                continue
            if current is None:
                raise ValueError(f"{self.dir}: no meta line")
            if "sp" in r:
                self.sps.setdefault(r.get("thr", 0), []).append(int(r["sp"], 16))
            if kind == "hit":
                for page, h in r["pages"]:
                    current[page] = int(h, 16)
                key = key_of(r)
                if key is not None and key not in self.points:
                    self.points[key] = (array.array("Q", current), r)
            elif kind in ("call", "ret"):
                key = key_of(r)
                if key is not None:
                    (self.calls if kind == "call" else self.rets).setdefault(key, {})[r["i"]] = r
            elif kind == "watch":
                self.watch.append(r)
        if self.meta is None:
            raise ValueError(f"{self.dir}: no meta line")
        self.canonical = self.meta.get("bytes") == "canonical-be"
        data = (self.dir / "pages.bin").read_bytes()
        self.store = data
        step = 16 + PAGE
        for at in range(0, len(data) - step + 1, step):
            self.offsets[struct.unpack_from("<Q", data, at + 8)[0]] = at + 16
        self.offsets.setdefault(page_hash(bytes(PAGE)), None)

    def page(self, h):
        at = self.offsets.get(h, -1)
        if at is None:
            return bytes(PAGE)
        if at < 0:
            raise ValueError(f"{self.dir}: page {h:016x} listed but not stored")
        return self.store[at:at + PAGE]


# ---- word equivalence -------------------------------------------------------------------------------------------
def near_float(ov, pv, ulps=4):
    """Both words are the same ordinary-magnitude float but for the last bits (MIPS vs x87/SSE rounding)."""
    if (ov ^ pv) & 0x80000000 or abs(ov - pv) > ulps:
        return False
    exponent = (ov >> 23) & 0xFF
    return 0x60 <= exponent <= 0x9F


class Matcher:
    def __init__(self, ram, symbols, exe_functions, exe_data=()):
        self.ram = ram
        self.sym_rows, self.sym_addr = symbols
        self.sym_keys = [a for a, _ in self.sym_rows]
        self.exe = exe_functions
        self.exe_keys = [a for a, *_ in exe_functions]
        self.original_functions = set()
        for _, _, name, _ in exe_functions:
            if name in self.sym_addr:
                self.original_functions.add(self.sym_addr[name])
        self.data = []
        for address, size, name, _ in sorted(exe_data):
            original = self.original_address(name)
            if original is not None:
                self.data.append((address, size, original))
        self.data_keys = [a for a, _, _ in self.data]
        self.stacks = ([], [])

    def set_stacks(self, oracle_sps, port_sps):
        """Every thread's stack span on each side, so a pointer to any thread's locals matches the other's."""
        def spans(sps, below, above):
            return [(min(v) - below, max(v) + above) for v in sps.values() if v and max(v) - min(v) < 0x100000]
        self.stacks = (spans(oracle_sps, PAGE, ORACLE_STACK), spans(port_sps, PAGE, PORT_STACK))

    def both_stack(self, oracle_value, port_value):
        return (any(lo <= oracle_value < hi for lo, hi in self.stacks[0])
                and any(lo <= port_value < hi for lo, hi in self.stacks[1]))

    def original_address(self, name):
        """The original address of a native data object: its symbol_addrs entry or its D_XXXXXXXX name."""
        name = name.split(".")[0]
        if name in self.sym_addr:
            return self.sym_addr[name]
        m = re.fullmatch(r"[A-Za-z]+_(8[0-9A-Fa-f]{7})", name)
        return int(m[1], 16) if m else None

    def port_data(self, value):
        """The original address of native address value inside a game data object (one past its end too), or None."""
        i = bisect.bisect_right(self.data_keys, value) - 1
        if i < 0:
            return None
        address, size, original = self.data[i]
        return original + value - address if value <= address + size else None

    def same_address(self, oracle_value, port_value):
        """The port value is the oracle's code or data address in native form."""
        return self.same_function(oracle_value, port_value) or (
            RDRAM <= oracle_value < RDRAM + self.ram and self.port_data(port_value) == oracle_value)

    def port_function(self, value):
        """The original address of the port function at native address value, or None."""
        i = bisect.bisect_right(self.exe_keys, value) - 1
        if i < 0:
            return None
        address, size, name, _ = self.exe[i]
        if value != address and not (size and value < address + size):
            return None
        return self.sym_addr.get(name, -1)

    def is_local(self, names):
        if not hasattr(self, "_local"):
            self._local = {name: local for _, _, name, local in self.exe}
        return any(self._local.get(n, False) for n in names)

    def same_function(self, oracle_value, port_value):
        if oracle_value not in self.original_functions:
            return False
        i = bisect.bisect_right(self.exe_keys, port_value) - 1
        if i < 0:
            return False
        address, size, _, _ = self.exe[i]
        if port_value != address and not (size and port_value < address + size):
            return False
        # Linker aliases share one native entry but may have distinct guest
        # addresses (for example a mapped callback label). Preserve every
        # symbol-proven identity instead of selecting the last equal-address name.
        while i >= 0 and self.exe[i][0] == address:
            if self.sym_addr.get(self.exe[i][2]) == oracle_value:
                return True
            i -= 1
        return False

    def host_pointer(self, oracle_value, port_value):
        in_ram = RDRAM <= port_value < RDRAM + self.ram
        return (not in_ram and port_value > 0xFFFF and self.port_function(port_value) is None
                and self.port_data(port_value) is None
                and (oracle_value == 0 or RDRAM <= oracle_value < RDRAM + self.ram))

    def symbol(self, address):
        i = bisect.bisect_right(self.sym_keys, address) - 1
        if i < 0:
            return f"0x{address:08x}"
        base, name = self.sym_rows[i]
        return f"{name}+0x{address - base:x}" if address != base else name

    def page_diff(self, oracle, port):
        """Word indices of the page that differ, each with its class (value or host-pointer)."""
        if oracle == port:
            return {}
        swapped = array.array("I", oracle)
        swapped.byteswap()
        if swapped.tobytes() == port:
            return {}
        result = {}
        skip = False
        for w in range(WORDS):
            if skip:
                skip = False
                continue
            c, q = oracle[4 * w:4 * w + 4], port[4 * w:4 * w + 4]
            if q == c or q == c[::-1] or q == c[1::-1] + c[2:] or q == c[:2] + c[:1:-1] or q == c[1::-1] + c[:1:-1]:
                continue
            ov, pv = int.from_bytes(c, "big"), int.from_bytes(q, "little")
            if self.same_address(ov, pv):
                continue
            if w % 2 == 0 and w + 1 < WORDS and port[4 * w:4 * w + 8] == oracle[4 * w:4 * w + 8][::-1]:
                skip = True
                continue
            if w % 2 == 1 and port[4 * w - 4:4 * w + 4] == oracle[4 * w - 4:4 * w + 4][::-1]:
                continue
            result[w] = "host-pointer" if self.host_pointer(ov, pv) else "fp-ulp" if near_float(ov, pv) else "value"
        return result


# ---- types (gcc -aux-info over the boot units) --------------------------------------------------------------------
BUILTIN = {
    "void": "void", "float": "float", "double": "double", "long double": "agg",
    "char": "int8", "signed char": "int8", "unsigned char": "uint8",
    "short": "int16", "short int": "int16", "signed short": "int16", "unsigned short": "uint16",
    "unsigned short int": "uint16", "int": "int32", "signed": "int32", "signed int": "int32", "unsigned": "int32",
    "unsigned int": "int32", "long": "int32", "long int": "int32", "unsigned long": "int32",
    "long unsigned int": "int32", "unsigned long int": "int32", "long long": "int64", "long long int": "int64",
    "unsigned long long": "int64", "long long unsigned int": "int64", "unsigned long long int": "int64",
    "_Bool": "uint8",
}
QUALIFIERS = {"const", "volatile", "register", "extern", "static", "inline", "__inline__", "__restrict", "restrict"}
DECL = re.compile(r"/\* (.*?):(\d+):([NO])([CF]) \*/ (.*?);")
TYPEDEF = re.compile(r"\btypedef\s+([^;{}()]+?)\s+(\w+)\s*(\[[^\]]*\])?\s*;")
AGG_TYPEDEF = re.compile(r"\btypedef\s+(?:struct|union)\b[^;]*?\}\s*(\w+)\s*;", re.DOTALL)


def kind_of(text, typedefs, depth=0):
    words = [w for w in text.replace("*", " * ").split() if w not in QUALIFIERS]
    if "*" in words or "[" in text or "(" in text:
        return "ptr"
    base = " ".join(words)
    if base in BUILTIN:
        return BUILTIN[base]
    if words and words[0] == "enum":
        return "int32"
    if words and words[0] in ("struct", "union"):
        return "agg"
    if base in typedefs and depth < 16:
        return kind_of(typedefs[base], typedefs, depth + 1)
    return "unknown"


def split_params(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch in "(["
        depth -= ch in ")]"
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def param_type(text, typedefs):
    """A parameter declaration without its name."""
    if "(" in text:
        return "ptr"
    words = text.replace("*", " * ").split()
    if len(words) >= 2 and words[-1] != "*" and re.fullmatch(r"\w+", words[-1]) and words[-1] not in BUILTIN \
            and not any(words[-1] in k.split() for k in BUILTIN) and words[-2] not in ("struct", "union", "enum"):
        words = words[:-1]
    return kind_of(" ".join(words), typedefs)


def parse_aux(text, wanted, typedefs, out):
    for m in DECL.finditer(text):
        decl = m[5]
        lp = decl.find("(")
        if lp < 0:
            continue
        head, params = decl[:lp].strip(), decl[lp + 1:decl.rfind(")")]
        name = head.split()[-1].lstrip("*") if head.split() else ""
        if name not in wanted or (name in out and m[4] == "C"):
            continue
        ret = kind_of(head[:len(head) - len(name)].strip(), typedefs)
        if m[3] == "O" or not params.strip():
            kinds = None if m[3] == "O" else []
        elif params.strip() == "void":
            kinds = []
        else:
            kinds = []
            for p in split_params(params):
                if p == "...":
                    kinds.append("var")
                    break
                kinds.append(param_type(p, typedefs))
        out[name] = (ret, kinds)


def build_types(build, trace, out_path, compiler, jobs):
    names, _ = read_trace(trace)
    wanted = {n for row in names.values() for n in row}
    linked = pathlib.Path(build) / "decomp-boot" / "linked-objects.txt"
    if linked.exists():
        units = [pathlib.Path(p[:-2] + ".i") for p in linked.read_text().split() if p.endswith(".o")]
    else:
        units = sorted((pathlib.Path(build) / "decomp").rglob("*.i"))
    units = [u for u in units if u.exists()]
    work = pathlib.Path(out_path).with_suffix(".aux")
    work.mkdir(parents=True, exist_ok=True)

    def one(index_unit):
        index, unit = index_unit
        target = work / f"{index}.txt"
        # Match the native decompilation build's language contract. GCC's newer
        # default rejects conversions accepted by that build, dropping its types.
        target.unlink(missing_ok=True)
        subprocess.run([compiler, "-m32", "-std=gnu89", "-fsyntax-only", "-w", "-aux-info", str(target), "-x", "cpp-output",
                        str(unit)], capture_output=True, text=True, check=True)
        return unit, target

    typedefs, found = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(one, enumerate(units)))
    for unit, _ in results[:50]:
        text = unit.read_text(errors="replace")
        for m in TYPEDEF.finditer(text):
            typedefs.setdefault(m[2], "void *" if m[3] else m[1])
        for m in AGG_TYPEDEF.finditer(text):
            typedefs.setdefault(m[1], "struct")
    for _, target in results:
        if target.exists():
            parse_aux(target.read_text(errors="replace"), wanted, typedefs, found)
    rows = ["name\treturn\tparams"]
    for name in sorted(found):
        ret, kinds = found[name]
        rows.append(f"{name}\t{ret}\t{'?' if kinds is None else ','.join(kinds)}")
    pathlib.Path(out_path).write_text("\n".join(rows) + "\n")
    return len(found), len(units)


def read_types(path):
    types = {}
    if path:
        for line in pathlib.Path(path).read_text().splitlines()[1:]:
            f = line.split("\t")
            if len(f) == 3:
                types[f[0]] = (f[1], None if f[2] == "?" else [k for k in f[2].split(",") if k])
    return types


# ---- calls ----------------------------------------------------------------------------------------------------------
def o32_slots(kinds):
    """Where each MIPS o32 argument lives: ('a', word) in a0..a3, ('f', 12|14), or None past the registers; and the
    byte offset of each in the port's i386 stack area."""
    where, port_at, word, fp_ok, fp_next, offset = [], [], 0, True, 12, 0
    for k in kinds:
        size = 8 if k in ("double", "int64") else 4
        if size == 8 and word % 2:
            word += 1
        if k in ("float", "double") and fp_ok and fp_next <= 14:
            where.append(("f", fp_next))
            fp_next += 2
        else:
            fp_ok = False
            where.append(("a", word) if word + size // 4 <= 4 else None)
        word += size // 4
        port_at.append(offset)
        offset += size
    return where, port_at


MASKS = {"int8": 0xFF, "uint8": 0xFF, "int16": 0xFFFF, "uint16": 0xFFFF}


def same_word(matcher, kind, ov, pv, osp, psp):
    m = MASKS.get(kind, 0xFFFFFFFF)
    if (ov & m) == (pv & m):
        return True
    if kind in ("ptr", "int32", "unknown"):
        if abs(ov - osp) < ORACLE_STACK and abs(pv - psp) < PORT_STACK or matcher.both_stack(ov, pv):
            return True
        return matcher.same_address(ov, pv)
    return False


def oracle_fields(kinds, ret_kind, call, returned):
    """Typed values in the reference ABI; never inspect unused registers."""
    fields = {}
    if call is not None and kinds is not None:
        where, _ = o32_slots(kinds)
        words = [int(x, 16) for x in call["a"]]
        for index, (kind, at) in enumerate(zip(kinds, where)):
            if kind in ("var", "agg") or at is None:
                break
            if at[0] == "f":
                value = int(call[f"f{at[1]}"], 16)
                if kind == "float":
                    value &= 0xFFFFFFFF
            elif kind in ("double", "int64"):
                value = words[at[1]] << 32 | words[at[1] + 1]
            else:
                value = words[at[1]] & MASKS.get(kind, 0xFFFFFFFF)
            fields[f"arg{index}"] = (kind, value)
    if returned is not None and ret_kind not in (None, "void", "agg", "unknown"):
        if ret_kind in ("float", "double"):
            value = int(returned["f0"], 16)
            if ret_kind == "float":
                value &= 0xFFFFFFFF
        elif ret_kind == "int64":
            value = int(returned["r"][0], 16) << 32 | int(returned["r"][1], 16)
        else:
            value = int(returned["r"][0], 16) & MASKS.get(ret_kind, 0xFFFFFFFF)
        fields["return"] = (ret_kind, value)
    return fields


def reference_call_changes(kinds, ret_kind, call, peer, returned=None, peer_returned=None):
    """Only fields observed and unequal in BOTH references are nondeterministic.

    Missing samples prove nothing. Stack-address differences alone do not make a
    field volatile, consistent with the call comparator's stack equivalence.
    """
    if call is None or peer is None:
        return {}
    first = oracle_fields(kinds, ret_kind, call, returned)
    second = oracle_fields(kinds, ret_kind, peer, peer_returned)
    sp, peer_sp = int(call["sp"], 16), int(peer["sp"], 16)
    changes = {}
    for field in first.keys() & second.keys():
        kind, a = first[field]
        _, b = second[field]
        if a == b:
            continue
        if kind in ("ptr", "int32", "unknown") and abs(a - sp) < ORACLE_STACK and abs(b - peer_sp) < ORACLE_STACK:
            continue
        changes[field] = {"field": field, "type": kind, "oracle": f"0x{a:x}", "oracle2": f"0x{b:x}"}
    return changes


def compare_call(matcher, kinds, local, oc, pc):
    """All differing arguments of one recorded call, in parameter order."""
    differences = []
    a = [int(x, 16) for x in oc["a"]]
    f = {12: int(oc["f12"], 16), 14: int(oc["f14"], 16)}
    words = [int(x, 16) for x in pc["a"]]
    osp, psp = int(oc["sp"], 16), int(pc["sp"], 16)
    if kinds is None:  # no prototype: the arity is unknown, so registers past it hold anything
        return differences
    where, port_at = o32_slots(kinds)
    for index, (k, at, pos) in enumerate(zip(kinds, where, port_at)):
        if k in ("var", "agg") or at is None or pos + (8 if k in ("double", "int64") else 4) > 32:
            break
        lo = words[pos // 4]
        hi = words[pos // 4 + 1] if pos // 4 + 1 < len(words) else 0
        if at[0] == "f":
            ov = f[at[1]] & 0xFFFFFFFF if k == "float" else f[at[1]]
            pv = lo if k == "float" else lo | hi << 32
        elif k in ("double", "int64"):
            ov, pv = a[at[1]] << 32 | a[at[1] + 1], lo | hi << 32
        else:
            ov, pv = a[at[1]], lo
        if k in ("double", "int64", "float") and ov != pv:
            differences.append(f"arg{index} {k} 0x{ov:x} vs port 0x{pv:x}")
        if k not in ("double", "int64", "float") and not same_word(matcher, k, ov, pv, osp, psp):
            differences.append(f"arg{index} {k} 0x{ov:08x} vs port 0x{pv:08x}")
    return differences


def compare_return(matcher, ret_kind, oret, pret, osp=0, psp=-PORT_STACK * 4):
    """The return's divergence, or None; pointers into the caller's stack (osp/psp: the call's sp) match."""
    if ret_kind in (None, "void", "agg", "unknown"):
        return None
    v0, v1 = (int(x, 16) for x in oret["r"])
    eax, edx = (int(x, 16) for x in pret["r"])
    if ret_kind in ("float", "double"):
        if pret.get("st0") is None:
            return f"return {ret_kind}: port left no st(0)"
        st0 = int(pret["st0"], 16)
        f0 = int(oret["f0"], 16)
        if ret_kind == "float":
            ov = f0 & 0xFFFFFFFF
            pv = struct.unpack("<I", struct.pack("<f", struct.unpack("<d", st0.to_bytes(8, "little"))[0]))[0]
        else:
            ov, pv = f0, st0
        return None if ov == pv else f"return {ret_kind} 0x{ov:x} vs port 0x{pv:x}"
    if ret_kind == "int64":
        ov, pv = v0 << 32 | v1, edx << 32 | eax
        return None if ov == pv else f"return int64 0x{ov:016x} vs port 0x{pv:016x}"
    if same_word(matcher, ret_kind, v0, eax, osp, psp):
        return None
    return f"return {ret_kind} 0x{v0:08x} vs port 0x{eax:08x}"


# ---- compare --------------------------------------------------------------------------------------------------------
def masked_ranges(args, matcher, oracle):
    """Masked byte ranges with reasons: guest stacks, ignore rows."""
    ranges = []
    for thread, sps in oracle.sps.items():
        lo, hi = min(sps), max(sps)
        if RDRAM <= lo < RDRAM + matcher.ram and hi - lo < 0x100000:
            ranges.append(((lo - PAGE) & ~(PAGE - 1), (hi + PAGE) & ~(PAGE - 1), f"guest stack of thread {thread}"))
    if args.ignore:
        keys = matcher.sym_keys
        for line in pathlib.Path(args.ignore).read_text().splitlines():
            f = line.split("\t")
            if not f[0].strip() or f[0].startswith("#"):
                continue
            reason = f[1] if len(f) > 1 else "ignored"
            m = re.fullmatch(r"(0x[0-9A-Fa-f]+)-(0x[0-9A-Fa-f]+)", f[0].strip())
            if m:
                ranges.append((int(m[1], 16), int(m[2], 16), reason))
            elif f[0].strip() in matcher.sym_addr:
                start = matcher.sym_addr[f[0].strip()]
                i = bisect.bisect_right(keys, start)
                end = keys[i] if i < len(keys) else start + 4
                ranges.append((start, min(end, start + 0x10000), reason))
    return ranges


def boot_unmask(diff, pg, oracle_page, boot, seen):
    """The page's differing words without those the oracle still holds as IPL3 copied them."""
    base = RDRAM + pg * PAGE - BOOT_COPY[0]
    if base + PAGE <= 0 or base >= len(boot):
        return diff
    out = {}
    for w, cls in diff.items():
        at = base + 4 * w
        if 0 <= at and at + 4 <= len(boot) and oracle_page[4 * w:4 * w + 4] == boot[at:at + 4]:
            seen.add(pg * WORDS + w)
        else:
            out[w] = cls
    return out


def words_text(data, w, big):
    out = []
    for k in range(w, min(w + 4, WORDS)):
        out.append(f"{int.from_bytes(data[4 * k:4 * k + 4], 'big' if big else 'little'):08x}")
    return " ".join(out)


ACCEPT_FIELDS = ("kind", "order", "function", "site", "detail", "reason", "reviewed_by")


def read_acceptances(path):
    """Strict exact-value review records; no range, wildcard, or silent masking."""
    with pathlib.Path(path).open() as stream:
        reader = csv.DictReader((line for line in stream if line.strip() and not line.startswith("#")), delimiter="\t")
        if reader.fieldnames != list(ACCEPT_FIELDS):
            raise ValueError("accept file must have columns: " + "\t".join(ACCEPT_FIELDS))
        rows, seen = [], set()
        for row in reader:
            if set(row) != set(ACCEPT_FIELDS) or any(not v or not v.strip() for v in row.values()):
                raise ValueError("accept row needs exact fields, a reason, and a reviewer")
            row["order"] = int(row["order"])
            if row["order"] < 1 or row["kind"] not in ("memory", "call"):
                raise ValueError("invalid accept kind or order")
            if row["kind"] == "memory":
                if not re.fullmatch(r"0x[0-9a-f]{8}", row["site"]) or not re.fullmatch(r"[0-9a-f]{8}/[0-9a-f]{8}", row["detail"]):
                    raise ValueError("memory acceptance needs an address and exact oracle/port words")
            elif not row["site"].isdigit():
                raise ValueError("call acceptance needs a call index")
            key = tuple(row[k] for k in ACCEPT_FIELDS[:5])
            if key in seen:
                raise ValueError("duplicate accept row")
            seen.add(key)
            rows.append(row)
    return rows


def apply_acceptances(report, reviews, common):
    lookup = {tuple(r[k] for k in ACCEPT_FIELDS[:5]): r for r in reviews}
    remaining, accepted, observed = [], [], set()
    for kind, rows in (("memory", report["memory"]), ("call", report["calls"])):
        for row in rows:
            row.pop("acceptance", None)
            site = row["address"] if kind == "memory" else str(row["call"])
            detail = row["oracle"].split()[0] + "/" + row["port"].split()[0] if kind == "memory" else row["why"]
            key = (kind, row["order"], row["function"], site, detail)
            review = lookup.get(key)
            if review:
                row["acceptance"] = {k: review[k] for k in ("reason", "reviewed_by")}
                accepted.append(dict(row, kind=kind))
                observed.add(key)
            else:
                remaining.append(dict(row, kind=kind))
    # Call before memory on a tie matches the existing raw-first rule.
    remaining.sort(key=lambda r: (r["order"], r["kind"] != "call"))
    report["first_after_accepted"] = remaining[0] if remaining else None
    bad = remaining[0]["order"] if remaining else None
    report["matched_through_after_accepted"] = max((k for k in common if bad is None or k < bad), default=0)
    report["accepted"] = sorted(accepted, key=lambda r: (r["order"], r["kind"] != "call"))
    report["unmatched_accepts"] = [r for key, r in lookup.items() if key not in observed]


def acceptance_text(row):
    review = row.get("acceptance")
    return f" [accepted: {review['reason']}; reviewed by {review['reviewed_by']}]" if review else ""


def compare(args):
    reviews = read_acceptances(args.accept)
    names, order_of = read_trace(args.trace)
    oracle = Side(args.oracle, lambda r: order_of.get(r.get("fn")) if "fn" in r else None)
    port = Side(args.port, lambda r: r.get("order"))
    if not oracle.canonical or port.canonical:
        raise ValueError("--oracle must be the canonical-be side and --port the native one")
    ram = min(oracle.meta["ram"], port.meta["ram"])
    matcher = Matcher(ram, read_symbols(args.symbols), read_exe_functions(args.port_exe), read_exe_data(args.port_exe))
    matcher.set_stacks(oracle.sps, port.sps)
    boot = read_boot_copy(args.rom)
    types = read_types(args.types)
    ranges = masked_ranges(args, matcher, oracle)
    masked_words = set()
    for lo, hi, _ in ranges:
        masked_words.update(range((max(lo, RDRAM) - RDRAM) // 4, (min(hi, RDRAM + ram) - RDRAM) // 4))
    volatile = set()
    other = None
    if args.oracle2:
        other = Side(args.oracle2, lambda r: order_of.get(r.get("fn")) if "fn" in r else None)
        for key in sorted(set(oracle.points) & set(other.points)):
            a, b = oracle.points[key][0], other.points[key][0]
            for pg in range(ram // PAGE):
                if a[pg] != b[pg]:
                    x, y = oracle.page(a[pg]), other.page(b[pg])
                    volatile.update(pg * WORDS + w for w in range(WORDS) if x[4 * w:4 * w + 4] != y[4 * w:4 * w + 4])
    common = sorted(set(oracle.points) & set(port.points))
    cache = {}
    per_point, boot_words = [], set()
    for key in common:
        a, b = oracle.points[key][0], port.points[key][0]
        diff = {}
        for pg in range(ram // PAGE):
            pair = (a[pg], b[pg])
            if pair not in cache:
                cache[pair] = matcher.page_diff(oracle.page(a[pg]), port.page(b[pg]))
                if boot is not None and cache[pair]:
                    cache[pair] = boot_unmask(cache[pair], pg, oracle.page(a[pg]), boot, boot_words)
            for w, cls in cache[pair].items():
                index = pg * WORDS + w
                if index not in masked_words and index not in volatile:
                    diff[index] = (cls, pair)
        per_point.append(diff)
    memory, shim, transient, setup, ulp = [], set(), 0, {}, set()

    def oracle_word(index, pair):
        w = index % WORDS
        return oracle.page(pair[0])[4 * w:4 * w + 4]

    for j, key in enumerate(common):
        previous = per_point[j - 1] if j else {}
        later = per_point[j + 1:j + 1 + args.persist]
        for index, (cls, pair) in sorted(per_point[j].items()):
            if j == 0 and key == min(names):
                # before the first game function: loader (IPL3) vs shim setup, not game behaviour; re-armed once
                # the game writes the word
                setup[index] = oracle_word(index, pair)
                continue
            if index in setup:
                if oracle_word(index, pair) == setup[index]:
                    continue
                del setup[index]
            elif index in previous:
                continue
            if cls == "host-pointer":
                shim.add(index)
                continue
            if cls == "fp-ulp":
                ulp.add(index)
                continue
            if not all(index in p for p in later):
                transient += 1
                continue
            memory.append((key, index, pair))
    calls, volatile_calls = [], []
    for key in common:
        row = names.get(key, ["?"])
        kinds_ret = next((types[n] for n in row if n in types), None)
        ret_kind, kinds = kinds_ret if kinds_ret else (None, None)
        local = matcher.is_local(row)
        for i in sorted(set(oracle.calls.get(key, {})) & set(port.calls.get(key, {}))):
            differences = compare_call(matcher, kinds, local, oracle.calls[key][i], port.calls[key][i])
            if i in oracle.rets.get(key, {}) and i in port.rets.get(key, {}):
                why = compare_return(matcher, ret_kind, oracle.rets[key][i], port.rets[key][i],
                                     int(oracle.calls[key][i]["sp"], 16), int(port.calls[key][i]["sp"], 16))
                if why:
                    differences.append(why)
            changes = reference_call_changes(
                kinds, ret_kind, oracle.calls[key][i], other.calls.get(key, {}).get(i),
                oracle.rets.get(key, {}).get(i), other.rets.get(key, {}).get(i)) if other else {}
            for why in differences:
                field = why.split()[0]
                if field in changes:
                    volatile_calls.append(dict(order=key, function=row[0], call=i, why=why,
                                               **changes[field]))
                else:
                    calls.append((key, i, why))
    first_mem = memory[0][0] if memory else None
    first_call = calls[0][0] if calls else None
    bad = min(x for x in (first_mem, first_call) if x is not None) if memory or calls else None
    matched = max([k for k in common if bad is None or k < bad], default=0)
    report = {
        "matched_through": matched, "of": args.pak, "port_points": len(port.points), "oracle_points": len(oracle.points),
        "compared": len(common), "trace": len(names), "first": None, "memory": [], "calls": [],
        "shim_words": len(shim), "transient_words": transient, "masked": [[f"0x{lo:08x}", f"0x{hi:08x}", why]
                                                                         for lo, hi, why in ranges],
        "volatile_words": len(volatile), "volatile_calls": volatile_calls, "boot_copy_words": len(boot_words),
        "setup_words": len(setup), "fp_ulp_words": len(ulp), "first_memory": None, "first_call": None, "watch": {"oracle": oracle.watch[:50], "port": port.watch[:50]},
    }
    for key, index, (oh, ph) in memory:
        address = RDRAM + index * 4
        w = index % WORDS
        report["memory"].append({
            "order": key, "function": names.get(key, ["?"])[0], "page": f"0x{address & ~(PAGE - 1):08x}",
            "address": f"0x{address:08x}", "symbol": matcher.symbol(address),
            "oracle": words_text(oracle.page(oh), w, True), "port": words_text(port.page(ph), w, False)})
    for key, i, why in calls:
        report["calls"].append({"order": key, "function": names.get(key, ["?"])[0], "call": i, "why": why})
    report["first_memory"] = dict(report["memory"][0], kind="memory") if report["memory"] else None
    report["first_call"] = dict(report["calls"][0], kind="call") if report["calls"] else None
    if bad is not None:
        if first_call is not None and (first_mem is None or first_call <= first_mem):
            report["first"] = dict(report["calls"][0], kind="call")
        else:
            report["first"] = dict(report["memory"][0], kind="memory")
    apply_acceptances(report, reviews, common)
    return report


def print_report(report):
    print(f"lockstep: matched-through {report['matched_through']}/{report['of']} "
          f"(port {report['port_points']} of {report['trace']} first hits, {report['compared']} compared, "
          f"oracle {report['oracle_points']})")
    first = report["first"]
    if first is None:
        print("lockstep: no divergence")
    elif first["kind"] == "call":
        print(f"lockstep: first divergence at order {first['order']} {first['function']} call {first['call']}: "
              f"{first['why']}")
    else:
        print(f"lockstep: first divergence at order {first['order']} {first['function']}: page {first['page']} "
              f"{first['symbol']}: oracle {first['oracle']} | port {first['port']}")
    if first is not None and first["kind"] == "memory" and report["first_call"]:
        c = report["first_call"]
        print(f"lockstep: first call divergence at order {c['order']} {c['function']} call {c['call']}: {c['why']}")
    after = report["first_after_accepted"]
    print("lockstep: first after accepted rows: " +
          (f"order {after['order']} {after['function']} {after.get('symbol', after.get('why', ''))}"
           if after else "none"))
    print(f"lockstep: matched-through after accepted rows {report['matched_through_after_accepted']}/{report['of']}")
    for row in report["memory"]:
        print(f"lockstep: memory order {row['order']} {row['function']} {row['symbol']} ({row['address']}): "
              f"oracle {row['oracle']} | port {row['port']}" + acceptance_text(row))
    for category in ("argument", "return"):
        for row in report["calls"]:
            if ("return" if row["why"].startswith("return ") else "argument") == category:
                print(f"lockstep: {category} order {row['order']} {row['function']} #{row['call']}: "
                      f"{row['why']}" + acceptance_text(row))
    for row in report.get("volatile_calls", []):
        print(f"lockstep: volatile call order {row['order']} {row['function']} #{row['call']}: "
              f"{row['why']} (independent references: {row['oracle']} vs {row['oracle2']})")
    for row in report["unmatched_accepts"]:
        print(f"lockstep: acceptance not observed: {row['kind']} order {row['order']} {row['function']} {row['site']}")
    print(f"lockstep: {len(report['memory'])} persistent memory words, {len(report['calls'])} call divergences listed; "
          f"{report['shim_words']} shim-owned words, {report['transient_words']} transient, "
          f"{report['volatile_words']} volatile, {report['boot_copy_words']} boot-copy masked, "
          f"{report['setup_words']} setup words (differ before the first game function), "
          f"{report['fp_ulp_words']} float words within 4 ulp")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("types", help="argument and return kinds of the trace functions (gcc -aux-info)")
    t.add_argument("--build", required=True)
    t.add_argument("--trace", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--compiler", default=os.environ.get("CC", "gcc"))
    t.add_argument("--jobs", type=int, default=4)
    c = sub.add_parser("compare", help="the first divergence and the matched-through number")
    c.add_argument("--oracle", required=True)
    c.add_argument("--port", required=True)
    c.add_argument("--trace", required=True)
    c.add_argument("--symbols")
    c.add_argument("--port-exe")
    c.add_argument("--types")
    c.add_argument("--ignore")
    c.add_argument("--oracle2")
    c.add_argument("--rom", help="the ROM, to mask IPL3's boot copy the port never makes")
    c.add_argument("--persist", type=int, default=2)
    c.add_argument("--pak", type=int, default=708)
    c.add_argument("--accept", default=str(pathlib.Path(__file__).with_name("lockstep-accept.tsv")),
                   help="reviewed exact differences with reasons (kept visible in the report)")
    c.add_argument("--json")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "types":
            found, units = build_types(args.build, args.trace, args.out, args.compiler, args.jobs)
            print(f"lockstep: types for {found} trace functions from {units} units -> {args.out}")
            return 0
        report = compare(args)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"lockstep: {error}", file=sys.stderr)
        return 2
    print_report(report)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
    return 1 if report["first_after_accepted"] else 0


if __name__ == "__main__":
    sys.exit(main())
