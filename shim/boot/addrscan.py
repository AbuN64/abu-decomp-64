# SPDX-License-Identifier: GPL-3.0-or-later
"""The static address-model scan: original addresses stored as data.

The game keeps code addresses in data (function tables, callbacks), often through its TLB map of the code
segment: a word 0x002791A8 is func_80279138_de at 0x802791A8 without the KSEG0 bit. Natively such a raw word is no
host address, so the first call through it faults (boot 102 calls 0x00239FBC). The source cures it by naming the
target (&func or a D_00XXXXXX alias the closure binds); where the source still holds the raw number, the ROM shows it:
the word in the cartridge equals a function's start (KSEG0) or its KUSEG alias, and the native image built from the
source holds the same number. image.py binds those words to the native function (or a generated stop the closure
then routes); this scan lists every such word before a boot does.

Classes of a 32-bit cartridge word w inside a data symbol (resident or overlay data with a rom: fact) or an
uncompressed blob fact:
  code      w is a function start (KSEG0) or a KUSEG alias of one (0x10000 <= w < 0x00800000, w | 0x80000000);
  tlb-data  w is the pinned address of a KUSEG data record (D_00XXXXXX: overlay data the game TLB-maps), which the
            closure binds at that virtual address and the shim maps when the game calls osMapTLB.
KSEG0 data addresses need no binding: resident data sits at its original address natively.
A word right after a display-list command head (top byte a GBI opcode) is a command argument, never a code pointer.
A KUSEG word that is no function start but lands up to MAX_SKIP instructions into one, with every skipped instruction a
register-only ALU op (the scheduled-into-prologue entry), is class code-entry and binds to that function.
A page-aligned KUSEG word also needs a pointer beside it: another code word or a KSEG0 RDRAM address within 6 words (a
table of callbacks, or a record that also holds pointers); alone, a value such as 0x00400000 is as likely a size or a
flag set as the alias of func_80400000_de. Off a page boundary a lone word is code (D_blob_* typed words included: the
image binds them like any data word). Raw binary blobs (audio, text) keep the pointer-beside rule for every word.
With --image (the build's decomp-boot directory) each code word inside the native image is reported bound (a
relocation of a name the source gives it, or a raw word the image bound: the native word differs from the cartridge's)
or raw (the native word is the cartridge's number); a raw code word, or one in a blob (no image rewrites blob bytes), is a need: one ADDRESS line,
and with --needs one JSON row; exit status 1 while any is left. Words of symbols the image does not place are listed as
unplaced (the native image holds nothing there)."""
import argparse
import bisect
import json
import pathlib
import re
import struct
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import link
import policy
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import portdir  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
KSEG0, KUSEG_END, KUSEG_LOW, RDRAM_END = 0x80000000, 0x00800000, 0x10000, 0x80800000
NEAR = tuple(k for k in range(-6, 7) if k)
# F3DEX2 command opcodes (the top byte of a display-list command's first word): geometry 0x01-0x07, the rest 0xD3-0xFF
GBI_HEADS = frozenset(range(0x01, 0x08)) | frozenset(range(0xD3, 0x100))
MAX_SKIP = 4  # instructions an entry alias may skip
# SPECIAL funct codes with register operands only: shifts, hi/lo moves, mult/div, add/sub/logic/set-less-than and
# their doubleword forms (no jr/jalr/syscall/break/sync/traps)
ALU_FUNCT = frozenset([0, 2, 3, 4, 6, 7, 16, 17, 18, 19, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38,
                       39, 42, 43, 44, 45, 46, 47, 56, 58, 59, 60, 62, 63])
ALU_IMMEDIATE = frozenset([8, 9, 10, 11, 12, 13, 14, 15, 24, 25])  # addi..lui, daddi, daddiu
TLB_RECORD = re.compile(r"D_(?:auto_)?00[0-9A-F]{6}")


class Targets:
    """Function starts and KUSEG data records of the version's pinned facts (image.facts)."""
    def __init__(self, table, preferred=(), roms=None):
        self.functions, self.records = {}, {}
        self.rom_of = {}  # function start (KSEG0) -> ROM offset of its first instruction, where the facts give one
        for name, (address, kind) in sorted(table.items()):
            if kind == "func" and address >= KSEG0:
                self.functions.setdefault(address, []).append(name)
            elif kind == "data" and address < KUSEG_END and TLB_RECORD.fullmatch(name):
                self.records.setdefault(address, name)
        self.preferred = set(preferred)
        for address, names in self.functions.items():
            for name in names:
                if roms and name in roms:
                    self.rom_of[address] = roms[name]
        self.starts = sorted(self.rom_of)

    def entry(self, word, rom):
        """(function, skipped instruction count) when the KUSEG word lands 1..MAX_SKIP instructions into a function
        and every skipped instruction is a register-only ALU op (the scheduled-into-prologue pattern), else None."""
        address = word | KSEG0
        i = bisect.bisect_right(self.starts, address) - 1
        if i < 0 or word & 3:
            return None
        start = self.starts[i]
        skipped = (address - start) // 4
        base = self.rom_of[start]
        if not 1 <= skipped <= MAX_SKIP or base + 4 * skipped > len(rom):
            return None
        for k in range(skipped):
            op, = struct.unpack_from(">I", rom, base + 4 * k)
            if not register_alu(op):
                return None
        name = self.function(start)
        return (name, skipped) if name else None

    def function(self, address):
        names = self.functions.get(address)
        if not names:
            return None
        linked = [name for name in names if name in self.preferred]
        return (linked or names)[0]

    def classify(self, word):
        """("code", function) | ("tlb-data", record) | None for one cartridge word."""
        if word >= KSEG0:
            name = self.function(word)
            return ("code", name) if name else None
        if KUSEG_LOW <= word < KUSEG_END:
            name = self.function(word | KSEG0)
            if name:
                return ("code", name)
            if word in self.records:
                return ("tlb-data", self.records[word])
        return None


def register_alu(instruction):
    """True for one MIPS instruction that only reads and writes registers (no load/store/branch/jump/call)."""
    op = instruction >> 26
    return (op == 0 and instruction & 0x3F in ALU_FUNCT) or op in ALU_IMMEDIATE


def _code(rom, at, targets, lo, hi):
    if not lo <= at <= hi - 4 or at + 4 > len(rom):
        return None
    word, = struct.unpack_from(">I", rom, at)
    found = targets.classify(word)
    if at - 4 >= lo and rom[at - 4] in GBI_HEADS:
        return None
    if found and found[0] == "code":
        return word, found[1], "exact"
    if not found and KUSEG_LOW <= word < KUSEG_END:
        entered = targets.entry(word, rom)
        if entered:
            return word, entered[0], "entry"
    return None


def _pointer(rom, at, targets, lo, hi):
    if not lo <= at <= hi - 4:
        return False
    word, = struct.unpack_from(">I", rom, at)
    return KSEG0 <= word < RDRAM_END or bool(_code(rom, at, targets, lo, hi))


def code_class(rom, at, targets, lo=0, hi=None, lone=True):
    """(function, class) of the 4-aligned cartridge word at , or None. class is "exact" (a function's start) or
    "entry" (a KUSEG alias a few register-only ALU instructions into one). None for a display-list command argument,
    or a KUSEG word that is neither near another pointer (a code word or a KSEG0 RDRAM address within 6 words inside
    [lo, hi)) nor (when lone) off a page boundary (alone, 0x00400000 is as likely a size; 0x0042F130 is no size). Raw
    binary blobs (audio, text) pass lone=False: a lone word there is as likely sample bytes."""
    hi = len(rom) if hi is None else hi
    found = _code(rom, at, targets, lo, hi)
    if not found:
        return None
    word, name, kind = found
    if word < KSEG0 and (word & 0xFFF == 0 or not lone) and not any(_pointer(rom, at + 4 * k, targets, lo, hi) for k in NEAR):
        return None
    return name, kind


def code_word(rom, at, targets, lo=0, hi=None):
    """The function the 4-aligned cartridge word at names (see code_class), or None."""
    found = code_class(rom, at, targets, lo, hi)
    return found[0] if found else None


def data_symbols(table, roms):
    """[(rom, address, size, name)] of the data symbols with a rom: fact, each up to the next one in the ROM."""
    rows = sorted((rom, table[name][0], name) for name, rom in roms.items()
                  if name in table and table[name][1] == "data" and table[name][0] >= KSEG0)
    out = []
    for i, (rom, address, name) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else rom + 4
        out.append((rom, address, max(0, min(end - rom, 0x10000)), name))
    return out


def _scan_word(rom, at, word, targets, lo, hi, lone=True):
    """(class, target) of one cartridge word: code, code-entry (an alias a few ALU instructions into a function),
    tlb-data; None when it is none of them."""
    found = targets.classify(word)
    if found and found[0] != "code":
        return found
    coded = code_class(rom, at, targets, lo, hi, lone)
    return ("code" if coded[1] == "exact" else "code-entry", coded[0]) if coded else None


def scan(rom, project, source):
    """[(rom offset, original address or None, word, class, target, container)] of every address-shaped word."""
    import image  # the build's own fact readers
    table = image.facts(source, project["version"])
    roms = image.rom_facts(source, project["version"])
    targets = Targets(table, roms=roms)
    out = []
    symbols = data_symbols(table, roms)
    for at, address, size, name in symbols:
        section = (0, len(rom))  # neighbours may sit in the symbols beside it (one word each)
        for offset in range(-(address % 4) % 4, size - 3, 4):
            if at + offset + 4 > len(rom):
                break
            word, = struct.unpack_from(">I", rom, at + offset)
            found = _scan_word(rom, at + offset, word, targets, *section)
            if found:
                out.append((at + offset, address + offset, word, found[0], found[1], name))
    for blob, (ranges, codec, _) in sorted(link.blob_ranges(source, project["version"]).items()):
        if codec not in (None, "none"):
            continue  # packed bytes: their pointers, if any, show in the unpacked layout's references
        for lo, hi in ranges:
            for at in range(lo + (-lo % 4), min(hi, len(rom)) - 3, 4):
                word, = struct.unpack_from(">I", rom, at)
                found = _scan_word(rom, at, word, targets, lo, hi, False)
                if found:
                    out.append((at, None, word, found[0], found[1], "blob " + blob))
    return out


def image_state(directory):
    """(extents [(address, offset, size)], image bytes, raw code words the image bound) of a built native image."""
    report = json.loads((directory / "image.json").read_text())
    text = (directory / "image.c").read_text()
    block = text.split("n64c_boot_image[] = {", 1)[1].split("};", 1)[0]
    extents = sorted((KSEG0 + int(a), int(b), int(z))
                     for a, b, z in re.findall(r"\{(\d+)u, (\d+)u, (\d+)u\}", block) if int(z))
    bound = {int(row["address"], 16) for row in report.get("code_words", [])}
    return extents, (directory / "image.bin").read_bytes(), bound


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--rom", type=pathlib.Path, required=True)
    parser.add_argument("--image", type=pathlib.Path, help="the build's decomp-boot directory")
    parser.add_argument("--needs", type=pathlib.Path, help="write one JSON row per unbound code word here")
    args = parser.parse_args()
    project = policy.load_project(args.manifest, args.source)
    rows = scan(args.rom.read_bytes(), project, args.source)
    extents, blob, bound = image_state(args.image) if args.image else ([], b"", set())
    starts = [start for start, _, _ in extents]
    counts, needs = {}, []
    for at, address, word, kind, target, container in rows:
        state = "found"
        if kind == "tlb-data":
            state = "virtual"
        elif address is None:
            state = "blob"
        elif args.image:
            i = bisect.bisect_right(starts, address) - 1
            if i < 0 or address + 4 > extents[i][0] + extents[i][2]:
                state = "unplaced"
            else:
                native, = struct.unpack_from("<I", blob, extents[i][1] + address - extents[i][0])
                state = "bound" if address in bound or native != word else "raw"
        counts[kind, state] = counts.get((kind, state), 0) + 1
        line = ["ADDRESS", f"0x{at:x}", f"0x{address:08x}" if address is not None else "-", f"0x{word:08x}", kind,
                target, state, container]
        if kind in ("code", "code-entry") and state in ("raw", "blob"):
            print(*line, sep="\t")
            needs.append({"kind": "address", "rom": f"0x{at:x}", "address": line[2], "word": line[3], "target": target,
                          "state": state, "container": container})
    for (kind, state), count in sorted(counts.items()):
        print("COUNT", kind, state, count, sep="\t")
    import closure  # a D_00XXXXXX name the closure cannot bind to its function routes as zeroed data
    facts = closure.Facts(args.source, HERE.parent, portdir.fact("shim-bindings.txt"), policy.game_code(project))
    for sym, address, names in facts.unbound_aliases():
        print("ADDRESS", "-", f"0x{address:08x}", "-", "alias", names[0], "unbound", sym, sep="\t")
        counts["alias", "unbound"] = counts.get(("alias", "unbound"), 0) + 1
        needs.append({"kind": "address", "rom": "-", "address": f"0x{address:08x}", "word": "-", "target": names[0],
                      "state": "alias-unbound", "container": sym})
    if args.needs:
        args.needs.write_text("".join(json.dumps(row) + "\n" for row in needs))
    return 1 if needs else 0


if __name__ == "__main__":
    sys.exit(main())
