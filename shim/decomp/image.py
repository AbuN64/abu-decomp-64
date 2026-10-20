# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the original resident data image from the decomp's own data units.

select: lists the function-free units that the pinned layout places in a resident
(non-overlay) section. build: reads their native objects, places every named
data symbol at its pinned original address and emits a link-time image (image.c, image.bin, image.ld, image.json) whose
pointer words are relocated (data targets to their original addresses, functions
to their native definitions or a generated fatal stop). Referenced names whose
original address lies inside the image are bound to that address. Symbols that
cannot be placed faithfully are refused with a reason and stay unresolved.
"""
import argparse
import json
import pathlib
import re
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import policy  # noqa: E402

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
FUNCTION = re.compile(r"INCLUDE_ASM|\)\s*\{|^\{", re.M)
OUTPUT = re.compile(r"^\s*(\.\w+)\s+(0x[0-9A-Fa-f]+)\s*:\s*AT\((0x[0-9A-Fa-f]+)\)")
INPUT = re.compile(r"^\s*(src/\S+?)\.o\(")
FACT = re.compile(r"(\w+)\s*=\s*(0x[0-9A-Fa-f]+);(.*)")
SHT_SYMTAB, SHT_NOBITS, SHT_REL, SHT_RELA = 2, 8, 9, 4
SHF_ALLOC, SHF_EXEC = 2, 4
STT_OBJECT, STT_FUNC, STT_SECTION = 1, 2, 3
R_386_NONE, R_386_32 = 0, 1


def select(source, exclude):
    placements, shared, current = {}, {}, None
    for line in (source / "RageWars.ld").read_text().splitlines():
        match = OUTPUT.match(line)
        if match:
            key = (match[1], int(match[2], 16))
            shared.setdefault(key, set()).add(int(match[3], 16))
            current = key
            continue
        match = INPUT.match(line)
        if match and match[1].endswith((".CPP", ".c")):
            placements.setdefault(match[1], set()).add(current)
    overlay = {key for key, lmas in shared.items() if len(lmas) > 1}
    units = []
    for unit, keys in sorted(placements.items()):
        if unit in exclude or keys & overlay:
            continue
        path = source / unit
        if path.is_file() and not FUNCTION.search(path.read_text(errors="replace")):
            units.append(unit)
    return units


def facts(source, version):
    table = {}
    for file in ("symbol_addrs.txt", "aliases.txt"):
        path = source / "versions" / version / file
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            match = FACT.match(line.strip())
            if not match:
                continue
            symbol, address = match[1], int(match[2], 16)
            kind = "func" if "type:func" in match[3] else "data"
            if table.get(symbol, (address, kind))[0] != address:
                raise ValueError("conflicting pinned address: " + symbol)
            table[symbol] = (address, kind)
    return table


class Elf:
    def __init__(self, path):
        data = path.read_bytes()
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            raise ValueError(f"{path}: not a little-endian ELF32 object")
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
        self.sections = []
        for index in range(shnum):
            fields = struct.unpack_from("<IIIIIIIIII", data, shoff + index * shentsize)
            self.sections.append(dict(zip(
                ("name", "type", "flags", "addr", "offset", "size", "link", "info", "align", "entsize"), fields)))
        names = self.sections[shstrndx]
        for section in self.sections:
            section["label"] = self._string(data, names, section["name"])
            section["bytes"] = b"" if section["type"] == SHT_NOBITS else \
                data[section["offset"]:section["offset"] + section["size"]]
        self.symbols, self.relocations = [], {}
        for section in self.sections:
            if section["type"] == SHT_SYMTAB:
                strings = self.sections[section["link"]]
                for offset in range(0, section["size"], 16):
                    name, value, size, info, _, shndx = struct.unpack_from("<IIIBBH", section["bytes"], offset)
                    self.symbols.append({"name": self._string(data, strings, name), "value": value,
                                         "size": size, "type": info & 15, "bind": info >> 4, "shndx": shndx})
        for section in self.sections:
            if section["type"] == SHT_RELA:
                raise ValueError(f"{path}: RELA relocations are not i386 objects")
            if section["type"] == SHT_REL:
                entries = self.relocations.setdefault(section["info"], [])
                for offset in range(0, section["size"], 8):
                    where, info = struct.unpack_from("<II", section["bytes"], offset)
                    entries.append((where, info >> 8, info & 255))

    @staticmethod
    def _string(data, table, offset):
        start = table["offset"] + offset
        return data[start:data.index(b"\0", start)].decode()

    def data_section(self, index):
        return 0 < index < len(self.sections) and self.sections[index]["flags"] & SHF_ALLOC \
            and not self.sections[index]["flags"] & SHF_EXEC


def build(manifest, source, objects, linked, output):
    project = policy.load_project(manifest, source)
    ram = int(project["rdram_bytes"], 0)
    table = facts(source, project["version"])
    claimed = set(project["data"]) | set(project["bindings"]) | set(project["stops"])
    linked_defined, linked_undefined = set(), set()
    for path in linked:
        for symbol in Elf(path).symbols:
            if symbol["name"] and symbol["bind"] in (1, 2):
                (linked_defined if symbol["shndx"] else linked_undefined).add(symbol["name"])
    resolved_names = linked_defined | set(project["bindings"]) | set(project["stops"])
    placed, refused = [], {}
    failed, code_units = [], []
    for path in objects:
        if not path.stat().st_size:
            failed.append(str(path))
            continue
        elf = Elf(path)
        if any(s["type"] == STT_FUNC and s["shndx"] for s in elf.symbols):
            code_units.append(str(path))  # functions arrive by include; not a data unit
            continue
        for symbol in elf.symbols:
            if symbol["type"] != STT_OBJECT or not elf.data_section(symbol["shndx"]) or not symbol["size"]:
                continue
            name = symbol["name"]
            if name not in table or table[name][1] != "data":
                refused[name] = "no pinned original data address"
                continue
            address = table[name][0]
            if not 0x80000000 <= address or address - 0x80000000 + symbol["size"] > ram:
                refused[name] = "outside mapped RDRAM"
                continue
            placed.append({"name": name, "address": address, "elf": elf, "symbol": symbol})
    placed.sort(key=lambda item: (item["address"], item["name"]))
    # Faithful placement: a native extent that reaches a later original symbol
    # has a different native layout, or two units claim one address.
    for index, item in enumerate(placed):
        end = item["address"] + item["symbol"]["size"]
        for later in placed[index + 1:]:
            if later["address"] >= end:
                break
            refused[item["name"]] = "native extent overlaps " + later["name"]
            refused.setdefault(later["name"], "native extent overlapped by " + item["name"])
    keep = [item for item in placed if item["name"] not in refused]
    stops, blob, words, extents, imaged = set(), bytearray(), [], [], 0

    def resolve(elf, symbol_index, addend):
        """A pointer word's value: an original address, or a native name."""
        target = elf.symbols[symbol_index]
        if target["type"] == STT_SECTION:
            for symbol in elf.symbols:
                if symbol["type"] == STT_OBJECT and symbol["shndx"] == target["shndx"] \
                        and symbol["value"] <= addend < symbol["value"] + max(symbol["size"], 1):
                    if symbol["name"] in table and table[symbol["name"]][1] == "data":
                        return ("addr", table[symbol["name"]][0] + addend - symbol["value"])
            return None
        name = target["name"]
        if name in resolved_names:
            return ("sym", name)
        if name in table and table[name][1] == "func":
            stops.add(name)
            return ("sym", name)
        if name in table:
            return ("addr", table[name][0] + addend)
        return None

    for item in keep:
        elf, symbol = item["elf"], item["symbol"]
        section = elf.sections[symbol["shndx"]]
        size = symbol["size"]
        chunk = bytearray(section["bytes"][symbol["value"]:symbol["value"] + size]) if section["bytes"] else bytearray(size)
        patches, failure = [], None
        for where, symbol_index, kind in elf.relocations.get(symbol["shndx"], []):
            if not symbol["value"] <= where < symbol["value"] + size:
                continue
            offset = where - symbol["value"]
            if kind != R_386_32 or offset + 4 > size:
                failure = "unsupported relocation kind %d" % kind
                break
            addend, = struct.unpack_from("<i", chunk, offset)
            value = resolve(elf, symbol_index, addend)
            if value is None:
                failure = "pointer to a name without a pinned address or native definition"
                break
            if value[0] == "addr":
                struct.pack_into("<I", chunk, offset, value[1] & 0xffffffff)
            else:
                patches.append((offset, value[1], addend))
        if failure:
            refused[item["name"]] = failure
            continue
        imaged += 1
        base = len(blob)
        blob += chunk
        words += [(base + offset, name, addend) for offset, name, addend in patches]
        if extents and extents[-1][0] + extents[-1][2] == item["address"]:
            extents[-1][2] += size
        else:
            extents.append([item["address"], base, size])
    stops -= resolved_names
    covered = lambda address: any(start <= address < start + size for start, _, size in extents)
    binds = {}
    for name in sorted(linked_undefined - linked_defined - claimed - stops):
        if name in table and table[name][1] == "data" and covered(table[name][0]):
            binds[name] = table[name][0]
    output.mkdir(parents=True, exist_ok=True)
    (output / "image.bin").write_bytes(bytes(blob))
    binary = (output / "image.bin").resolve()
    asm = ['.section .rodata.n64c_boot_image,"a"', ".globl n64c_boot_image_bytes", ".balign 4",
           "n64c_boot_image_bytes:"]
    cursor = 0
    for offset, name, addend in sorted(words):
        if offset > cursor:
            asm.append(f'.incbin "{binary}", {cursor}, {offset - cursor}')
        asm.append(f".long {name}{addend:+d}" if addend else f".long {name}")
        cursor = offset + 4
    if cursor < len(blob):
        asm.append(f'.incbin "{binary}", {cursor}, {len(blob) - cursor}')
    asm.append(".previous")
    code = ["/* Generated original data image: bytes and pointer words of the fetched source objects. */",
            '#include "boot.h"', 'extern void n64c_boot_missing(const char *) __attribute__((noreturn));',
            "__asm__(", *[f'    "{line.replace(chr(34), chr(92) + chr(34))}\\n"' for line in asm], ");"]
    code += [f'void {name}(void) {{ n64c_boot_missing("{name}"); }}' for name in sorted(stops)]
    code.append("const N64cBootImage n64c_boot_image[] = {")
    code += [f"    {{{start - 0x80000000}u, {base}u, {size}u}}," for start, base, size in extents]
    code += ["    {0u, 0u, 0u}", "};", f"const size_t n64c_boot_image_count = {len(extents)};"]
    (output / "image.c").write_text("\n".join(code) + "\n")
    (output / "image.ld").write_text("".join(f"{name} = 0x{address:08x};\n" for name, address in binds.items())
                                     + "SECTIONS {} INSERT AFTER .text;\n")
    report = {"symbols": imaged,
              "bytes": len(blob), "extents": len(extents), "relocated_words": len(words),
              "failed_units": failed, "code_units": code_units, "function_stops": sorted(stops), "bound": sorted(binds), "refused": dict(sorted(refused.items()))}
    (output / "image.json").write_text(json.dumps(report, indent=1) + "\n")


def compile_unit(prepare, compiler, source, output, flags):
    """Compile one image unit; a unit that does not compile contributes nothing.

    Its diagnostics go to OUTPUT.err and OUTPUT is left empty, so an unrelated
    source failure never blocks the image (build lists it under failed_units)."""
    base = output.with_suffix("")
    steps = [[sys.executable, "-I", str(prepare), "--compiler", compiler, "--source", str(source),
              "--output", f"{base}.i", "--stubs", f"{base}.stubs.c", "--inventory", f"{base}.json",
              "--depfile", f"{output}.d", "--", *flags],
             [compiler, *flags, "-x", "cpp-output", "-c", f"{base}.i", "-o", str(output)]]
    error, depfile = pathlib.Path(f"{output}.err"), pathlib.Path(f"{output}.d")
    output.parent.mkdir(parents=True, exist_ok=True)
    failure = None
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True)
        if result.returncode:
            failure = result.stderr[-4000:] or "failed\n"
            break
    # The preprocessor's header list names this object, so a header fix
    # rebuilds a failed unit too.
    headers = depfile.read_text().split(":", 1)[1] if depfile.exists() else f" {source}\n"
    depfile.write_text(f"{output}:{headers}")
    if failure:
        output.write_bytes(b"")
        error.write_text(failure)
    elif error.exists():
        error.unlink()


def lines(path):
    return [pathlib.Path(line) for line in path.read_text().split() if line]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pick = sub.add_parser("select")
    pick.add_argument("--source", type=pathlib.Path, required=True)
    pick.add_argument("--manifest", type=pathlib.Path, required=True)
    unit = sub.add_parser("compile")
    unit.add_argument("--prepare", type=pathlib.Path, required=True)
    unit.add_argument("--compiler", required=True)
    unit.add_argument("--source", type=pathlib.Path, required=True)
    unit.add_argument("--output", type=pathlib.Path, required=True)
    unit.add_argument("flags", nargs=argparse.REMAINDER)
    make = sub.add_parser("build")
    make.add_argument("--manifest", type=pathlib.Path, required=True)
    make.add_argument("--source", type=pathlib.Path, required=True)
    make.add_argument("--objects", type=pathlib.Path, required=True, help="file listing image unit objects")
    make.add_argument("--linked", type=pathlib.Path, required=True, help="file listing linked boot objects")
    make.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "select":
            exclude = set(policy.load_project(args.manifest, args.source)["sources"])
            print(";".join(select(args.source, exclude)))
        elif args.command == "compile":
            flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
            compile_unit(args.prepare, args.compiler, args.source, args.output, flags)
        else:
            build(args.manifest, args.source, lines(args.objects), lines(args.linked), args.output)
    except (ValueError, KeyError, OSError, struct.error) as error:
        parser.exit(2, f"boot-image: {error}\n")
