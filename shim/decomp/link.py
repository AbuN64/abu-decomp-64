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
TLB_RECORD = re.compile(r"D_(?:auto_)?00[0-9A-F]{6}\Z")
NAMED_ADDRESS = re.compile(r"D_auto_(8[0-9A-F]{7})\Z")
# Byte-order descriptor kinds of a ROM range (n64c_pi.h): raw bytes, an array of 16-bit or 32-bit values, or repeated
# records described by a field string of widths in bytes ("4 4 2222 444"), or back-to-back records of different sizes
# whose tag (1, 2 or 4 bytes, big-endian, at a fixed offset) picks a field string ("tagged"), or an audio control bank
# container the game reads in place, whose record graph the PI layer walks for its field widths ("alctl").
# u16/u32 are the older spellings. Unpacked RNC streams may also be a "graph" (n64c_pi.h GRAPH): named types, each a
# record ("fields": widths and "@<section>:<Type>" offset references), a "leaf" (stride, count, records), or a
# "directory" (offset table container; a list of section types, {"each": Type} for all, or tag-selected types), a
# tag-selected record ("variants"), a head record followed by element records to the extent's end ("head_then"), a
# leaf stored as byte planes that the game transposes into records ("planes": the record type), or an RNC stream
# nested in the bytes ("packed": the type of its unpacked bytes, or null).
KINDS = {"raw": "N64C_RES_RAW", "swap16": "N64C_RES_SWAP16", "swap32": "N64C_RES_SWAP32", "fields": "N64C_RES_STRUCT",
         "tagged": "N64C_RES_TAGGED", "alctl": "N64C_RES_ALCTL", "u16": "N64C_RES_SWAP16", "u32": "N64C_RES_SWAP32",
         "graph": "N64C_RES_GRAPH"}
WIDTHS = {"raw": 1, "swap16": 2, "swap32": 4, "u16": 2, "u32": 4, "tagged": 1, "alctl": 1, "graph": 1}
UNPACKED_KINDS = {"raw", "swap16", "swap32", "fields", "tagged", "graph"}
MAX_LAYOUTS = 16  # n64c_pi.c keeps 16 layouts per tagged resource
FIELD_WIDTH = re.compile(r"[1-8]")
MAX_FIELDS = 63  # n64c_pi.c keeps a 64-byte field string
GRAPH_REF = re.compile(r"@(\d+):([A-Za-z_][A-Za-z_0-9]*)\Z")
BLOB_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
RNC_CODECS = ("rnc1", "rnc2")


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
def graph_program(item, label):
    """The n64c_pi GRAPH program of a "graph" row: its root type first, the others in name order."""
    types, root = item.get("types"), item.get("root")
    if item.get("fields") is not None or not isinstance(types, dict) or root not in types:
        raise ValueError("invalid graph: " + label)
    order = [root] + sorted(name for name in types if name != root)
    index = {name: i for i, name in enumerate(order)}

    def ref(name, what):
        if name not in index:
            raise ValueError(f"graph {what} names no type: {label}: {name}")
        return str(index[name])
    def tag_pairs(value, label, name):
        tag, layouts = value.get("tag"), value.get("layouts")
        if set(value) != {"tag", "layouts"} or not isinstance(tag, dict) or set(tag) != {"offset", "width"} or \
                tag["width"] not in (1, 2, 4) or type(tag["offset"]) is not int or tag["offset"] < 0 or \
                not isinstance(layouts, dict) or not layouts:
            raise ValueError(f"invalid tag selection: {label}: {name}")
        pairs = []
        for key, part in layouts.items():
            if not key.isdecimal() or int(key) > 0xffffffff or str(int(key)) != key:
                raise ValueError(f"invalid layout tag: {label}: {name}")
            pairs.append(f"{key}={ref(part, 'layout')}")
        return f'{tag["offset"]}/{tag["width"]}:' + ",".join(pairs)
    program = []
    for name in order:
        body = types[name]
        if not isinstance(body, dict) or len(body) != 1:
            raise ValueError(f"invalid graph type: {label}: {name}")
        (shape, value), = body.items()
        if shape == "fields":
            items = value.split() if isinstance(value, str) else []
            if not items:
                raise ValueError(f"empty graph record: {label}: {name}")
            out = []
            for token in items:
                match = GRAPH_REF.fullmatch(token)
                if match:
                    out.append(f"@{int(match[1])}:{ref(match[2], 'reference')}")
                elif FIELD_WIDTH.fullmatch(token) or re.fullmatch(r"[1-8]+", token):
                    out.append(token)
                else:
                    raise ValueError(f"invalid graph field: {label}: {name}: {token}")
            program.append("F:" + " ".join(out))
        elif shape == "leaf":
            if not isinstance(value, str) or not {"fields", "variants"} & set(types.get(value, {})):
                raise ValueError(f"graph leaf needs a record type: {label}: {name}")
            program.append("L:" + ref(value, "leaf"))
        elif shape == "planes":
            if not isinstance(value, str) or "fields" not in types.get(value, {}) or "@" in types[value]["fields"]:
                raise ValueError(f"graph planes needs a record type without references: {label}: {name}")
            program.append("X:" + ref(value, "planes"))
        elif shape == "packed" and (value is None or isinstance(value, str)):
            program.append("P:" + ("-" if value is None else ref(value, "packed")))
        elif shape == "head_then" and isinstance(value, list) and len(value) == 2 and \
                all("fields" in types.get(part, {}) for part in value if isinstance(part, str)):
            program.append("R:" + ",".join(ref(part, "record") for part in value))
        elif shape == "variants" and isinstance(value, dict) and set(value) == {"tag", "layouts"} and \
                all("fields" in types.get(part, {}) for part in value["layouts"].values() if isinstance(part, str)):
            program.append("T:" + tag_pairs(value, label, name))
        elif shape == "directory" and isinstance(value, list) and value:
            program.append("D:" + ",".join("-" if part is None else ref(part, "section") for part in value))
        elif shape == "directory" and isinstance(value, dict) and set(value) == {"each"}:
            program.append("E:" + ref(value["each"], "section"))
        elif shape == "directory" and isinstance(value, dict):
            program.append("G:" + tag_pairs(value, label, name))
        else:
            raise ValueError(f"invalid graph type: {label}: {name}")
    return ";".join(program)


def descriptor(item, label, unpacked=False):
    """The (kind, fields, record bytes) of a loads/resources row; raises on anything the PI layer cannot fix up."""
    kind = item["kind"]
    if kind not in KINDS or kind == "graph" and not unpacked:
        raise ValueError("unsupported untyped/pointer layout: " + label)
    if unpacked and kind not in UNPACKED_KINDS:
        raise ValueError("unsupported unpacked layout: " + label)
    if kind == "graph":
        return kind, graph_program(item, label), 1
    if item.get("root") is not None or item.get("types") is not None:
        raise ValueError("root/types given for a " + kind + " descriptor: " + label)
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


def blob_facts(source, version):
    """{blob name: (rom, bytes, codec, format)} of the blobs the decomp's blob facts (blobs.toml) place in this
    version's ROM as one range."""
    import tomllib  # Python 3.11+, only where blob facts are read

    path = source / "blobs.toml"
    if not path.exists():
        return {}
    facts = {}
    for blob, row in tomllib.loads(path.read_text()).get("blob", {}).items():
        ranges = row.get("rom", {}).get(version)
        if not ranges:
            continue
        if len(ranges) != 1 or len(ranges[0]) != 2:
            if row.get("codec") in RNC_CODECS:
                raise ValueError("invalid RNC blob fact: " + blob)
            continue
        lo, hi = (number(value) for value in ranges[0])
        if row.get("codec") in RNC_CODECS and (not BLOB_NAME.fullmatch(blob) or hi - lo < 18):
            raise ValueError("invalid RNC blob fact: " + blob)
        facts[blob] = (lo, hi - lo, row.get("codec"), row.get("format"))
    return facts


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
        # A D_00XXXXXX record the game reaches through its own TLB mapping sits at its KUSEG virtual address
        # (below the host image, where the shim's osMapTLB maps guest pages); anything else lives in RDRAM.
        tlb_mapped = TLB_RECORD.fullmatch(symbol) and 0x10000 <= value < 0x08000000
        if not tlb_mapped and not 0x80000000 <= value <= 0x80000000 + ram:
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
    # Blob layouts, keyed by the decomp's blob facts (blobs.toml): an RNC stream's row types its unpacked bytes (the PI
    # layer serves every stream's header in host order; streams without a row unpack raw), an uncompressed blob's row
    # types its ROM bytes like a resources row.
    # A row names one blob ("blob") or every blob of a format ("format", the blob facts' format string).
    facts = blob_facts(source, project["version"])
    streams = {blob: (rom, size) for blob, (rom, size, codec, _) in facts.items() if codec in RNC_CODECS}
    layouts = {}
    for item in project.get("blob_layouts", []):
        if ("blob" in item) == ("format" in item):
            raise ValueError("blob layout needs one of blob or format")
        label = item.get("blob") or item["format"]
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ValueError("missing layout evidence: " + str(label))
        named = [item["blob"]] if "blob" in item else sorted(b for b, fact in facts.items() if fact[3] == item["format"])
        if not named or any(blob not in facts or blob in layouts for blob in named):
            raise ValueError(f"blob layout names no single-range blob fact of {project['version']} (or twice): {label}")
        layout = descriptor({k: v for k, v in item.items() if k not in ("blob", "format")}, str(label), unpacked=True)
        for blob in named:
            layouts[blob] = layout
    for blob in sorted(layouts, key=lambda name: facts[name][0]):
        if blob in streams:
            continue
        rom, size, _, _ = facts[blob]
        kind, fields, width = layouts[blob]
        if not BLOB_NAME.fullmatch(blob) or rom + size > rom_bytes or size % width:
            raise ValueError("invalid blob range: " + blob)
        for old_lo, old_hi, old_name in rom_ranges:
            if rom < old_hi and rom + size > old_lo:
                raise ValueError(f"overlapping resource range: {blob} and {old_name}")
        rom_ranges.append((rom, rom + size, blob))
        code.insert(code.index('    {0},  /* keeps the array non-empty; the count below excludes it */'),
                    f'    {{"{blob}", {rom}u, 0u, {size}u, {KINDS[kind]}, {c_string(fields)}}},')
    code.append('const N64cBootLoad n64c_boot_packed[] = {')
    for blob, (rom, size) in sorted(streams.items(), key=lambda pair: pair[1]):
        if rom + size > rom_bytes:
            raise ValueError("RNC blob outside the cartridge: " + blob)
        for old_lo, old_hi, old_name in rom_ranges:
            if rom < old_hi and rom + size > old_lo:
                raise ValueError(f"overlapping resource range: {blob} and {old_name}")
        rom_ranges.append((rom, rom + size, blob))
        kind, fields, _ = layouts.get(blob, ("raw", None, 1))
        code.append(f'    {{"{blob}", {rom}u, 0u, {size}u, {KINDS[kind]}, {c_string(fields)}}},')
    code += ['    {0},  /* keeps the array non-empty; the count below excludes it */', '};',
             'const size_t n64c_boot_packed_count = sizeof n64c_boot_packed / sizeof n64c_boot_packed[0] - 1;']
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
