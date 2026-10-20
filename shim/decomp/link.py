# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate explicit project bindings and emit native link/load inputs."""
import argparse
import json
import pathlib
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import policy  # noqa: E402

IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
# The source's address-named data (D_auto_80XXXXXX): the name states its address, the pinned facts may omit it.
NAMED_ADDRESS = re.compile(r"D_auto_(8[0-9A-F]{7})\Z")
# Byte-order descriptor kinds of a ROM range (n64c_pi.h): raw bytes, an array of 16-bit or 32-bit values, or repeated
# records described by a field string of widths in bytes ("4 4 2222 444"), or back-to-back records of different sizes
# whose tag (1, 2 or 4 bytes, big-endian, at a fixed offset) picks a field string ("tagged"). u16/u32 are the older spellings.
KINDS = {"raw": "N64C_RES_RAW", "swap16": "N64C_RES_SWAP16", "swap32": "N64C_RES_SWAP32", "fields": "N64C_RES_STRUCT",
         "tagged": "N64C_RES_TAGGED", "u16": "N64C_RES_SWAP16", "u32": "N64C_RES_SWAP32"}
WIDTHS = {"raw": 1, "swap16": 2, "swap32": 4, "u16": 2, "u32": 4, "tagged": 1}
MAX_LAYOUTS = 16  # n64c_pi.c keeps 16 layouts per tagged resource
FIELD_WIDTH = re.compile(r"[1-8]")
MAX_FIELDS = 63  # n64c_pi.c keeps a 64-byte field string


def name(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError("invalid C symbol")
    return value


def c_string(text):
    return "0" if text is None else '"' + text + '"'


def field_widths(text, label):
    widths = [c for c in text if c != " "] if isinstance(text, str) else []
    if not widths or len(widths) > MAX_FIELDS or not all(FIELD_WIDTH.fullmatch(c) for c in widths) or \
            not re.fullmatch(r"[1-8]+( [1-8]+)*", text.strip()):
        raise ValueError("invalid field widths: " + label)
    return widths
def tagged_spec(item, label):
    """The "offset/width;tag=fields;..." string of a tagged resource: records of different sizes, back to back."""
    tag, layouts = item.get("tag"), item.get("layouts")
    if item.get("fields") is not None or not isinstance(tag, dict) or set(tag) != {"offset", "width"} or \
            tag["width"] not in (1, 2, 4) or type(tag["offset"]) is not int or tag["offset"] < 0:
        raise ValueError("invalid tag: " + label)
    if not isinstance(layouts, dict) or not 0 < len(layouts) <= MAX_LAYOUTS:
        raise ValueError("invalid layouts: " + label)
    parts = [f'{tag["offset"]}/{tag["width"]}']
    for key, text in layouts.items():
        if not isinstance(key, str) or not key.isdecimal() or int(key) > 0xffffffff or str(int(key)) != key:
            raise ValueError("invalid layout tag: " + label)
        record = sum(int(c) for c in field_widths(text, label))
        if record < tag["offset"] + tag["width"]:
            raise ValueError("layout shorter than its tag: " + label)
        parts.append(f"{key}={text.strip()}")
    return ";".join(parts)
def descriptor(item, label):
    """The (kind, fields, record bytes) of a loads/resources row; raises on anything the PI layer cannot fix up."""
    kind = item["kind"]
    if kind not in KINDS:
        raise ValueError("unsupported untyped/pointer layout: " + label)
    if kind == "tagged":
        return kind, tagged_spec(item, label), 1
    if item.get("layouts") is not None or item.get("tag") is not None:
        raise ValueError("tag/layouts given for a " + kind + " descriptor: " + label)
    if kind != "fields":
        if item.get("fields") is not None:
            raise ValueError("fields given for a " + kind + " descriptor: " + label)
        return kind, None, WIDTHS[kind]
    text = item.get("fields")
    if not isinstance(text, str):
        raise ValueError("missing field widths: " + label)
    widths = [c for c in text if c != " "]
    if not widths or len(widths) > MAX_FIELDS or not all(FIELD_WIDTH.fullmatch(c) for c in widths) or \
            not re.fullmatch(r"[1-8]+( [1-8]+)*", text.strip()):
        raise ValueError("invalid field widths: " + label)
    return kind, text.strip(), sum(int(c) for c in widths)


def number(value):
    result = int(value, 0) if isinstance(value, str) else value
    if type(result) is not int or not 0 <= result <= 0xffffffff:
        raise ValueError("invalid 32-bit value")
    return result


def generate(manifest, source, output):
    project = policy.load_project(manifest, source)
    pin = project["source_pin"]
    if not re.fullmatch(r"[a-f0-9]{40}", pin):
        raise ValueError("invalid source pin")
    symbols = {name: int(address, 16) for name, (address, _) in policy.pinned(source, project["version"]).items()}
    ram = number(project["rdram_bytes"])
    clock = number(project["video_clock_hz"])
    if ram not in (0x400000, 0x800000) or not clock:
        raise ValueError("invalid memory configuration")
    rom_bytes = number(project["rom_bytes"])
    rom_digest = project["rom_sha256"]
    if not 0 < rom_bytes <= 0x0fc00000 or not re.fullmatch(r"[a-f0-9]{64}", rom_digest):
        raise ValueError("invalid ROM identity")
    bindings, owners = [], set()
    def claim(symbol):
        name(symbol)
        if symbol in owners:
            raise ValueError("duplicate symbol owner: " + symbol)
        owners.add(symbol)
    for symbol, address in project["data"].items():
        claim(symbol)
        value = number(address)
        named = NAMED_ADDRESS.match(symbol)
        pinned = symbols.get(symbol, int(named[1], 16) if named else None)
        if pinned != value:
            raise ValueError("symbol address differs from pinned facts: " + symbol)
        if not 0x80000000 <= value <= 0x80000000 + ram:
            raise ValueError("data outside mapped RDRAM: " + symbol)
        bindings.append(f"{symbol} = 0x{value:08x};")
    # Cartridge-offset names (the game ORs them with the PI base): absolute values inside the ROM image.
    for symbol, offset in project.get("rom_offsets", {}).items():
        claim(symbol)
        value = number(offset)
        if symbols.get(symbol) != value:
            raise ValueError("symbol offset differs from pinned facts: " + symbol)
        if not value < rom_bytes:
            raise ValueError("ROM offset outside the cartridge: " + symbol)
        bindings.append(f"{symbol} = 0x{value:08x};")
    for symbol, target in project["bindings"].items():
        claim(symbol)
        # EXTERN makes the linker pull a target from an archive (host libgcc, the shim) for this script.
        bindings.append(f"EXTERN({name(target)})" +
                        (f" {symbol} = {name(target)};" if symbol != target else ""))
    code = ['/* Generated explicit bindings; no original game code. */', '#include "boot.h"',
            'extern void n64c_boot_missing(const char *) __attribute__((noreturn));']
    for symbol, evidence in project["stops"].items():
        claim(symbol)
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("missing stop evidence: " + symbol)
        code.append(f'void {symbol}(void) {{ n64c_boot_missing("{symbol}"); }}')
    entry = name(project["entry"])
    code += [f'extern int {entry}(void);', f'int n64c_boot_entry(void) {{ return {entry}(); }}',
             f'const u32 n64c_boot_ram_bytes = {ram}u;', f'const u32 n64c_boot_video_clock = {clock}u;',
             f'const u32 n64c_boot_rom_bytes = {rom_bytes}u;',
             f'const char n64c_boot_rom_sha256[] = "{rom_digest}";',
             'const N64cBootLoad n64c_boot_loads[] = {']
    ranges, rom_ranges = [], []
    for load in project["loads"]:
        symbol = name(load["symbol"])
        if symbol not in project["data"]:
            raise ValueError("unbound load: " + symbol)
        rom, size = number(load["rom"]), number(load["bytes"])
        physical = number(project["data"][symbol]) - 0x80000000
        if not size or physical + size > ram or rom + size > rom_bytes:
            raise ValueError("invalid load range: " + symbol)
        if not load.get("evidence"):
            raise ValueError("missing layout evidence: " + symbol)
        kind, fields, width = descriptor(load, symbol)
        if size % width:
            raise ValueError("partial load field: " + symbol)
        for old_rom, old_ram, old_size in ranges:
            if rom < old_rom + old_size and rom + size > old_rom or physical < old_ram + old_size and physical + size > old_ram:
                raise ValueError("overlapping load range: " + symbol)
        ranges.append((rom, physical, size))
        rom_ranges.append((rom, rom + size, symbol))
        code.append(f'    {{"{symbol}", {rom}u, {physical}u, {size}u, {KINDS[kind]}, {c_string(fields)}}},')
    if not ranges:
        raise ValueError("empty boot image")
    code += ['};', 'const size_t n64c_boot_load_count = sizeof n64c_boot_loads / sizeof n64c_boot_loads[0];']
    # ROM resources the game DMAs itself later (archive headers, tables, models): only the PI byte-order
    # descriptor is registered; the range is never copied at start-up. Anything not listed stays raw.
    code.append('const N64cBootLoad n64c_boot_resources[] = {')
    for item in project.get("resources", []):
        label = name(item["name"])
        rom, size = number(item["rom"]), number(item["bytes"])
        if not size or rom + size > rom_bytes:
            raise ValueError("invalid resource range: " + label)
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ValueError("missing layout evidence: " + label)
        kind, fields, width = descriptor(item, label)
        if size % width:
            raise ValueError("partial resource record: " + label)
        for old_lo, old_hi, old_name in rom_ranges:
            if rom < old_hi and rom + size > old_lo:
                raise ValueError(f"overlapping resource range: {label} and {old_name}")
        rom_ranges.append((rom, rom + size, label))
        code.append(f'    {{"{label}", {rom}u, 0u, {size}u, {KINDS[kind]}, {c_string(fields)}}},')
    code += ['    {0},  /* keeps the array non-empty; the count below excludes it */', '};',
             'const size_t n64c_boot_resource_count = sizeof n64c_boot_resources / sizeof n64c_boot_resources[0] - 1;']
    system = project.get("system")
    if system:
        fields = {"initialized": ("u32", 4), "tv_type": ("u32", 4), "reset_type": ("u32", 4),
                  "memory_size": ("u32", 4), "video_clock": ("u32", 4),
                  "clock_rate": ("u64", 8), "nmi_buffer": ("u8", 64)}
        if set(system["globals"]) != set(fields):
            raise ValueError("incomplete system globals")
        tv, reset = number(system["tv_type"]), number(system["reset_type"])
        if tv > 2 or reset > 1:
            raise ValueError("invalid boot values")
        code.append('const N64cSystemGlobals n64c_boot_system = {')
        occupied = []
        for field, (ctype, width) in fields.items():
            symbol = name(system["globals"][field])
            address = number(project["data"][symbol])
            if address % min(width, 4) or address + width > 0x80000000 + ram:
                raise ValueError("invalid system field range: " + field)
            if any(address < old + size and address + width > old for old, size in occupied):
                raise ValueError("overlapping system fields")
            occupied.append((address, width))
            code.append(f'    .{field} = ({ctype} *)0x{address:08x}u,')
        code += ['};', f'const u32 n64c_boot_tv_type = {tv}u;', f'const u32 n64c_boot_reset_type = {reset}u;']
    else:
        code += ['const N64cSystemGlobals n64c_boot_system = {0};',
                 'const u32 n64c_boot_tv_type = 0, n64c_boot_reset_type = 0;']

    flavours = {"audio": 0, "audio_ge": 1, "audio_bc": 2, "naudio": 3}  # N64aFlavour order
    audio = project.get("audio_abi")
    if audio is None:
        code.append("const int n64c_boot_audio_abi = -1;")
    else:
        if audio.get("flavour") not in flavours or not audio.get("evidence"):
            raise ValueError("audio ABI needs a known flavour and evidence")
        code.append(f"const int n64c_boot_audio_abi = {flavours[audio['flavour']]};")

    output.mkdir(parents=True, exist_ok=True)
    (output / "bindings.ld").write_text("\n".join(bindings) + "\nSECTIONS {} INSERT AFTER .text;\n")
    (output / "bindings.c").write_text("\n".join(code) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.manifest, args.source, args.output)
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(2, f"boot-bindings: {error}\n")
