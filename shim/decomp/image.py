# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the original resident data image from the decomp's own data units.

select: lists the function-free units that the pinned layout places in a resident
(non-overlay) section, then those it places in a shared-address overlay section for the
project's version. build: reads their native objects, places every named
data symbol at its pinned original address and emits a link-time image (image.c, image.bin, image.ld, image.json) whose
pointer words are relocated (data targets to their original addresses, functions
to their native definitions or a generated fatal stop). Referenced names whose
original address lies inside the image are bound to that address. Symbols that
cannot be placed faithfully are refused with a reason and stay unresolved.
Overlay data has no fixed RDRAM home (the game DMAs the overlay wherever it runs it, e.g. into
memory it then TLB-maps): its symbols are keyed by their ROM offset instead (section LMA plus
the symbol's offset from the section VMA) and the PI layer writes their native bytes over every
DMA of that ROM range (n64c_pi_register_image), so the types the source gives them decide the
byte order the native reads see.
Raw code addresses (with --rom, the cartridge): a word the source keeps as a plain number that the cartridge shows to
be a code address (addrscan.code_word: a function's start, or its KUSEG alias with a pointer beside it) holds that
number natively too, and a call through it faults. The image binds such a word to the function like a relocation
(its native definition or a generated stop the closure then routes) and lists it in image.json code_words.
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
import addrscan
import policy  # noqa: E402

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
FUNCTION = re.compile(r"INCLUDE_ASM|\)\s*\{|^\{", re.M)
OUTPUT = re.compile(r"^\s*(\.\w+)\s+(0x[0-9A-Fa-f]+)\s*:\s*AT\((0x[0-9A-Fa-f]+)\)")
INPUT = re.compile(r"^\s*(src/\S+?)\.o\(")
FACT = re.compile(r"(\w+)\s*=\s*(0x[0-9A-Fa-f]+);(.*)")
INITIALISED = re.compile(r"\.(?:s?data|rodata)\b")
# Names the data emitter gives bytes it has not typed (hash blobs, padding, unresolved spans, code words kept as a
# data array under the function's name): an overlay keeps its ROM bytes there, since a native word array of them
# would only swap texels, code and strings.
PLACEHOLDER = re.compile(r"D_(?:blob|auto)_\w+|D_d\d+_[0-9a-f]+|func_\w+")
ROM_FACT = re.compile(r"\brom:(0x[0-9A-Fa-f]+)")
CONDITION = re.compile(r"^\s*#\s*(if|elif|else|endif)\b(.*)")
TERM = re.compile(r"(!?)\s*defined\s*\(\s*(\w+)\s*\)")
SHT_SYMTAB, SHT_NOBITS, SHT_REL, SHT_RELA = 2, 8, 9, 4
SHF_ALLOC, SHF_EXEC = 2, 4
STT_OBJECT, STT_FUNC, STT_SECTION = 1, 2, 3
R_386_NONE, R_386_32 = 0, 1


def _active(expression, macro):
    """A layout #if/#elif condition: defined(NAME) or !defined(NAME) terms joined by ||, for one version macro."""
    value = False
    for term in expression.split("||"):
        match = TERM.fullmatch(term.strip())
        if not match:
            raise ValueError("unsupported layout condition: " + expression.strip())
        value |= (match[2] == macro) != bool(match[1])
    return value


def placements(source, version=None):
    """(unit, section key, LMA, input spec) per input line; with a version, only its lines and sections."""
    macro = "VERSION_" + version.upper().replace("-", "_") if version else None
    stack, current, rows = [], None, []
    for line in (source / "RageWars.ld").read_text().splitlines():
        condition = CONDITION.match(line)
        if condition and macro:
            word, rest = condition[1], condition[2]
            if word == "if":
                parent = all(frame[0] for frame in stack)
                value = parent and _active(rest, macro)
                stack.append([value, value])
            elif word == "elif":
                if not stack:
                    raise ValueError("#elif without #if in the layout")
                parent = all(frame[0] for frame in stack[:-1])
                value = parent and not stack[-1][1] and _active(rest, macro)
                stack[-1] = [value, stack[-1][1] or value]
            elif word == "else":
                if not stack:
                    raise ValueError("#else without #if in the layout")
                parent = all(frame[0] for frame in stack[:-1])
                stack[-1] = [parent and not stack[-1][1], True]
            elif not stack:
                raise ValueError("#endif without #if in the layout")
            else:
                stack.pop()
            continue
        if macro and not all(frame[0] for frame in stack):
            continue
        match = OUTPUT.match(line)
        if match:
            current = ((match[1], int(match[2], 16)), int(match[3], 16))
            continue
        match = INPUT.match(line)
        if match and match[1].endswith((".CPP", ".c")):
            rows.append((match[1], *(current or (None, None)), line[match.end():]))
    return rows


def overlay_keys(source):
    """Sections sharing one name and VMA at several LMAs: shared-address overlays."""
    shared = {}
    for line in (source / "RageWars.ld").read_text().splitlines():
        match = OUTPUT.match(line)
        if match:
            shared.setdefault((match[1], int(match[2], 16)), set()).add(int(match[3], 16))
    return {key for key, lmas in shared.items() if len(lmas) > 1}


def function_free(path):
    return path.is_file() and not FUNCTION.search(path.read_text(errors="replace"))


def overlay_units(source, version):
    """Overlay data units of one version: unit -> (VMA, LMA) of the overlay section holding its initialised data."""
    overlay, units = overlay_keys(source), {}
    for unit, key, lma, spec in placements(source, version):
        if key in overlay and INITIALISED.search(spec) and not re.search(r"bss|COMMON", spec) \
                and units.setdefault(unit, (key[1], lma)) != (key[1], lma):
            raise ValueError("overlay data unit in two sections: " + unit)
    return units


def resident_units(source, version):
    """Resident data units of one version: unit -> (VMA, LMA) of the first section holding its initialised data (where
    its ROM bytes sit, for the raw code address check)."""
    overlay, units = overlay_keys(source), {}
    for unit, key, lma, spec in placements(source, version):
        if key not in overlay and INITIALISED.search(spec) and not re.search(r"bss|COMMON", spec):
            units.setdefault(unit, (key[1], lma))
    return units


def select(source, exclude, version=None):
    keys = {}
    for unit, key, _, _ in placements(source):
        keys.setdefault(unit, set()).add(key)
    overlay = overlay_keys(source)
    units = [unit for unit, unit_keys in sorted(keys.items())
             if unit not in exclude and not unit_keys & overlay and function_free(source / unit)]
    if version:
        units += [unit for unit in sorted(overlay_units(source, version))
                  if unit not in exclude and function_free(source / unit)]
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
    # A KUSEG alias of a function (a data name such as D_00XXXXXX or rw_menu_paged_func_804402DC_de pinned at a
    # function's address without its KSEG0 bit, the game's pointers into TLB-mapped code) is a code pointer: a word
    # naming it binds to the function, never to the raw value.
    functions = {address for address, kind in table.values() if kind == "func"}
    for symbol, (address, kind) in table.items():
        if kind == "data" and 0x10000 <= address < 0x00800000 and (address | 0x80000000) in functions:
            table[symbol] = (address, "func")
    return table


def rom_facts(source, version):
    """symbol -> ROM offset from the pinned facts' rom: annotations."""
    roms = {}
    for file in ("symbol_addrs.txt", "aliases.txt"):
        path = source / "versions" / version / file
        if path.exists():
            for line in path.read_text().splitlines():
                match, rom = FACT.match(line.strip()), ROM_FACT.search(line)
                if match and rom:
                    roms[match[1]] = int(rom[1], 16)
    return roms


def overlapping(items, key, refused):
    """Faithful placement: a native extent that reaches a later original symbol has a different
    native layout, or two units claim one place."""
    items.sort(key=lambda item: (item[key], item["name"]))
    for index, item in enumerate(items):
        end = item[key] + item["symbol"]["size"]
        for later in items[index + 1:]:
            if later[key] >= end:
                break
            refused[item["name"]] = "native extent overlaps " + later["name"]
            refused.setdefault(later["name"], "native extent overlapped by " + item["name"])
    return [item for item in items if item["name"] not in refused]


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


def build(manifest, source, objects, linked, output, rom=None):
    project = policy.load_project(manifest, source)
    ram = int(project["rdram_bytes"], 0)
    table = facts(source, project["version"])
    overlays, roms = overlay_units(source, project["version"]), rom_facts(source, project["version"])
    residents = resident_units(source, project["version"]) if rom is not None else {}
    claimed = set(project["data"]) | set(project["bindings"]) | set(project["stops"])
    linked_defined, linked_undefined = set(), set()
    for path in linked:
        for symbol in Elf(path).symbols:
            if symbol["name"] and symbol["bind"] in (1, 2):
                (linked_defined if symbol["shndx"] else linked_undefined).add(symbol["name"])
    resolved_names = linked_defined | set(project["bindings"]) | set(project["stops"])
    placed, overlay_placed, refused = [], [], {}
    failed, code_units = [], []
    for path in objects:
        if not path.stat().st_size:
            failed.append(str(path))
            continue
        section_of = next((overlays[unit] for unit in overlays if path.as_posix().endswith("/" + unit + ".o")), None)
        resident_of = next((residents[unit] for unit in residents if path.as_posix().endswith("/" + unit + ".o")), None)
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
            if section_of:
                vma, lma = section_of
                if elf.sections[symbol["shndx"]]["type"] == SHT_NOBITS:
                    refused[name] = "overlay bss has no ROM bytes"
                elif PLACEHOLDER.fullmatch(name):
                    refused[name] = "untyped emitter placeholder: the overlay keeps its ROM bytes"
                elif address < vma:
                    refused[name] = "pinned below its overlay section"
                elif roms.get(name, lma + address - vma) != lma + address - vma:
                    refused[name] = "overlay ROM offset differs from its rom fact"
                else:
                    overlay_placed.append({"name": name, "rom": lma + address - vma, "elf": elf, "symbol": symbol})
                continue
            if not 0x80000000 <= address or address - 0x80000000 + symbol["size"] > ram:
                refused[name] = "outside mapped RDRAM"
                continue
            item = {"name": name, "address": address, "elf": elf, "symbol": symbol}
            if name not in roms and resident_of and address >= resident_of[0]:
                item["image_rom"] = resident_of[1] + address - resident_of[0]  # its ROM bytes, by its section
            placed.append(item)
    keep = overlapping(placed, "address", refused)
    keep_overlay = overlapping(overlay_placed, "rom", refused)
    stops, blob, words, extents, rom_extents, imaged = set(), bytearray(), [], [], [], 0

    targets = addrscan.Targets(table, resolved_names, roms)

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
        if name in table and table[name][1] == "func" and table[name][0] < 0x80000000:
            # a KUSEG alias (D_00XXXXXX) names the function at its KSEG0 address: bind to that function, or stop on
            # its name so the closure routes the function (a stop named after the alias names no unit to link)
            function = targets.function(table[name][0] | 0x80000000)
            if function:
                if function not in resolved_names:
                    stops.add(function)
                return ("sym", function)
        if name in table and table[name][1] == "func":
            stops.add(name)
            return ("sym", name)
        if name in table:
            return ("addr", table[name][0] + addend)
        return None

    code_words = []
    def raw_code(item, chunk, taken):
        """Bind the words of chunk the cartridge shows to be code addresses kept as plain numbers."""
        at = item.get("rom", roms.get(item["name"], item.get("image_rom")))
        if rom is None or at is None:
            return []
        start = item.get("address", item.get("rom"))
        out = []
        for offset in range(-start % 4, len(chunk) - 3, 4):
            if offset in taken or at + offset + 4 > len(rom):
                continue
            word, = struct.unpack_from(">I", rom, at + offset)
            if struct.unpack_from("<I", chunk, offset)[0] != word:
                continue  # the source types these bytes otherwise (not one native word equal to the cartridge's)
            found = addrscan.code_class(rom, at + offset, targets)
            if not found:
                continue
            name = found[0]
            if name not in resolved_names:
                stops.add(name)
            out.append((offset, name, 0))
            code_words.append({"symbol": item["name"], "address": f"0x{start + offset:08x}",
                               "rom": f"0x{at + offset:x}", "word": f"0x{word:08x}", "function": name,
                               "class": found[1]})
        return out

    for item in keep + keep_overlay:
        elf, symbol = item["elf"], item["symbol"]
        section = elf.sections[symbol["shndx"]]
        size = symbol["size"]
        chunk = bytearray(section["bytes"][symbol["value"]:symbol["value"] + size]) if section["bytes"] else bytearray(size)
        patches, failure, taken = [], None, set()
        for where, symbol_index, kind in elf.relocations.get(symbol["shndx"], []):
            if not symbol["value"] <= where < symbol["value"] + size:
                continue
            offset = where - symbol["value"]
            taken.add(offset)
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
        patches += raw_code(item, chunk, taken)
        imaged += 1
        base = len(blob)
        blob += chunk
        words += [(base + offset, name, addend) for offset, name, addend in patches]
        runs, start = (rom_extents, item["rom"]) if "rom" in item else (extents, item["address"])
        if runs and runs[-1][0] + runs[-1][2] == start and runs[-1][1] + runs[-1][2] == base:
            runs[-1][2] += size
        else:
            runs.append([start, base, size])
    stops -= resolved_names
    covered = lambda address: any(start <= address < start + size for start, _, size in extents)
    binds = {}
    for name in sorted(linked_undefined - linked_defined - claimed - stops):
        if name in table and table[name][1] == "data" and covered(table[name][0]):
            binds[name] = table[name][0]
    # A bound code word makes its function live, and --gc-sections then keeps that function's callees too: an
    # unnamed game function (func_*) no linked unit defines gets a weak fatal stop (a real definition or a closure
    # binding still wins). Named callees are left out: libultra names come from the shim library, whose archive
    # members a weak definition would keep from being pulled in.
    weak = sorted(name for name in linked_undefined - linked_defined - claimed - stops
                  if name.startswith("func_") and table.get(name, (0, ""))[1] == "func")
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
            '#include "boot.h"', 'extern long long n64c_boot_missing(const char *);',
            "__asm__(", *[f'    "{line.replace(chr(34), chr(92) + chr(34))}\\n"' for line in asm], ");"]
    code += [f'long long {name}(void) {{ return n64c_boot_missing("{name}"); }}' for name in sorted(stops)]
    code += [f'__attribute__((weak)) long long {name}(void) {{ return n64c_boot_missing("{name}"); }}'
             for name in weak]
    code.append("const N64cBootImage n64c_boot_image[] = {")
    code += [f"    {{{start - 0x80000000}u, {base}u, {size}u}}," for start, base, size in extents]
    code += ["    {0u, 0u, 0u}", "};", f"const size_t n64c_boot_image_count = {len(extents)};"]
    code.append("const N64cBootRomImage n64c_boot_rom_image[] = {")
    code += [f"    {{0x{start:x}u, {base}u, {size}u}}," for start, base, size in rom_extents]
    code += ["    {0u, 0u, 0u}", "};", f"const size_t n64c_boot_rom_image_count = {len(rom_extents)};"]
    (output / "image.c").write_text("\n".join(code) + "\n")
    (output / "image.ld").write_text("".join(f"{name} = 0x{address:08x};\n" for name, address in binds.items())
                                     + "SECTIONS {} INSERT AFTER .text;\n")
    report = {"symbols": imaged,
              "bytes": len(blob), "extents": len(extents), "relocated_words": len(words),
              "overlay_symbols": sorted(item["name"] for item in keep_overlay if item["name"] not in refused),
              "rom_extents": [[f"0x{start:x}", size] for start, _, size in rom_extents],
              "failed_units": failed, "code_units": code_units, "function_stops": sorted(stops), "bound": sorted(binds), "refused": dict(sorted(refused.items())),
              "rom_checked": rom is not None, "code_words": code_words, "weak_stops": weak}
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
    make.add_argument("--rom", type=pathlib.Path, help="the cartridge: bind raw code addresses it shows")
    args = parser.parse_args()
    try:
        if args.command == "select":
            project = policy.load_project(args.manifest, args.source)
            print(";".join(select(args.source, set(project["sources"]), project["version"])))
        elif args.command == "compile":
            flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
            compile_unit(args.prepare, args.compiler, args.source, args.output, flags)
        else:
            build(args.manifest, args.source, lines(args.objects), lines(args.linked), args.output,
                  args.rom.read_bytes() if args.rom else None)
    except (ValueError, KeyError, OSError, struct.error) as error:
        parser.exit(2, f"boot-image: {error}\n")
