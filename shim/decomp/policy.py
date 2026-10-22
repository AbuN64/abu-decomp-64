# SPDX-License-Identifier: GPL-3.0-or-later
"""Select clean boot inputs; original SDK bodies are never executable inputs."""
import argparse
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SDK_LO, SDK_HI = 0x802AE000, 0x802C8000
PIN = re.compile(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;(.*)")


def pinned(source, version):
    """The decomp's pinned facts for a version: {name: (address, comment)}. symbol_addrs.txt names every
    symbol; aliases.txt adds a second name for pinned storage (D_801470C0_de = 0x8014D340: the de-named
    buffer at its us-rev1 address). A data alias the table lacks counts as pinned; a function alias does not,
    so aliases never move a function into or out of the libultra range."""
    table = {}
    for line in (source / "versions" / version / "symbol_addrs.txt").read_text().splitlines():
        if (match := PIN.match(line)):
            table[match[1]] = (match[2], match[3])
    aliases = source / "versions" / version / "aliases.txt"
    for line in aliases.read_text().splitlines() if aliases.exists() else ():
        if (match := PIN.match(line)) and match[1].startswith("D_"):
            if match[1] in table and int(table[match[1]][0], 16) != int(match[2], 16):
                raise ValueError("conflicting pinned address: " + match[1])
            table.setdefault(match[1], (match[2], match[3]))
    # RW's fuzzy drafts keep a file-local view of a global (`#define D_x fz_D_x`: its own type, the same storage):
    # fz_D_x is pinned wherever D_x is.
    for name in [n for n in table if n.startswith("D_")]:
        table.setdefault("fz_" + name, table[name])
    return table


def game_code(project):
    """Game functions the project places inside the libultra address range (manifest `game_in_sdk_range`:
    name -> evidence). They are game code: never a shim row, and their units link like any game unit."""
    return frozenset(project.get("game_in_sdk_range", {}))


def sdk_address(symbol, addresses, game=frozenset()):
    if symbol.startswith("D_") or symbol in game:
        return None
    match = re.search(r"func_(8[0-9A-F]{7})", symbol)
    address = addresses.get(symbol, int(match[1], 16) if match else None)
    return address if address is not None and SDK_LO <= address < SDK_HI else None


def functions(text):
    # Inspect declarations only. No original implementation is emitted.
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    bodies = re.findall(r"^[A-Za-z_][^;{}=\n]*?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", text, re.M)
    asm = re.findall(r"INCLUDE_ASM\([^)]*,\s*([A-Za-z_]\w*)\s*\)", text)
    return sorted(set(bodies + asm))


def sdk_unit(unit, text, addresses, game=frozenset()):
    names = functions(text)
    if names and set(names) <= game:
        return False  # a unit holding only project-declared game code, wherever the decomp filed it
    return "/sdk/" in unit.lower() or any(sdk_address(fn, addresses, game) for fn in names)


def sanitize(project, source, addresses, bindings, shim_text):
    selected = []
    game = game_code(project)
    for unit in project.get("sources", []):
        text = (source / unit).read_text()
        if not sdk_unit(unit, text, addresses, game):
            selected.append(unit)
            continue
        for symbol in functions(text):
            target = bindings.get(symbol)
            if target and re.search(r"\b" + re.escape(target) + r"\s*\(", shim_text):
                project["stops"].pop(symbol, None)
                project["bindings"][symbol] = target
            elif symbol not in project["bindings"]:
                project["stops"][symbol] = f"original SDK unit {unit}; clean shim required (SDK: shim binding decision)"
    if "sources" in project:
        project["sources"] = selected
    return project


def load_project(manifest, source):
    project = json.loads(manifest.read_text())
    addresses = {name: int(address, 16) for name, (address, _) in pinned(source, project["version"]).items()}
    bindings = {}
    for line in (HERE / "shim-bindings.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            symbol, target, *_ = line.split()
            bindings[symbol] = target
    shim_text = "\n".join(p.read_text() for p in HERE.parent.glob("n64c_*.[ch]"))
    return sanitize(project, source, addresses, bindings, shim_text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    args = parser.parse_args()
    print(json.dumps(load_project(args.manifest, args.source)))
