"""Build a candidate C file into a flat image the Cake Taste `cases` probe can recompile.

The compile is the project's own (the build context of the pinned unbake: preprocess, compile and assemble with
the file's toolchain and flags). The object is then linked with the host's linker at a free address, every other
symbol resolving to its address in the version's symbol table, and the linked sections become one flat image plus
a function table. The object's functions are the candidate; everything they call stays the original.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abu_decomp import DecompError
from abu_decomp.behave import names
from abu_decomp.config import Profile
from abu_decomp.tools import api_call, invoke, settings

LOADABLE = (".text", ".rodata", ".data")


class CandidateError(DecompError):
    """The candidate cannot be built or linked; the reason is the finding, the verdict is `untestable`."""


@dataclass(frozen=True)
class Image:
    path: Path
    vram: int
    functions: tuple[dict[str, Any], ...]
    size: int
    object_sha256: str
    # siblings with an original address that still run from the image: code branches to them PC-relative, which
    # cannot reach their original address (see pc_targets)
    kept: tuple[str, ...] = ()

    def spec(self, entry: str) -> dict[str, Any]:
        return {"image": str(self.path), "vram": self.vram, "functions": list(self.functions), "entry": entry}


def host_tools(host: Path) -> dict[str, str]:
    """The MIPS binutils the unbake host file names."""
    try:
        table = tomllib.loads(host.read_text())["tools"]
        return {key: table[key] for key in ("mips_ld", "mips_objcopy", "mips_objdump")}
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise CandidateError(
            "behave.host", f"{host}: needs tools.mips_ld, mips_objcopy and mips_objdump ({exc})"
        ) from exc


def symbol_table(profile: Profile, version: str, project: Path | None, host: Path | None) -> dict[str, list[Any]]:
    """name -> [vram, rom, size, type] for one version, through `unbake symbols`."""
    called = invoke("unbake", profile, ["symbols", "--version", version], project=project, host=host)
    if not called.ok:
        raise CandidateError("behave.symbols", f"unbake symbols failed: {called.document.get('findings')}")
    return called.document["result"]["symbols"][version]


def all_symbol_tables(profile: Profile, project: Path | None, host: Path | None) -> dict[str, dict[str, list[Any]]]:
    """version -> name -> [vram, rom, size, type] for every version of the project."""
    called = invoke("unbake", profile, ["symbols"], project=project, host=host)
    if not called.ok:
        raise CandidateError("behave.symbols", f"unbake symbols failed: {called.document.get('findings')}")
    return called.document["result"]["symbols"]


_ASSIGN = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*=\s*(0[xX][0-9a-fA-F]+)\s*;")


def auto_symbols(project: Path, version: str) -> dict[str, int]:
    """The addresses the project's own link defines outside its symbol table: build/<version>/undefined_funcs_auto.txt
    and undefined_syms_auto.txt (linker assignments the build generates for targets nothing names)."""
    found: dict[str, int] = {}
    for name in ("undefined_funcs_auto.txt", "undefined_syms_auto.txt"):
        try:
            lines = (project / "build" / version / name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            match = _ASSIGN.match(line)
            if match:
                found[match[1]] = int(match[2], 16)
    return found


_AUTO_NAME = re.compile(r"^D_auto_([0-9A-Fa-f]{8})$")


def named_addresses(names: list[str]) -> dict[str, int]:
    """Data labels the build's unbake names after their own address (`D_auto_800D7068`, a label inside an unclaimed
    asm blob): the real link defines them from the asm, a candidate object is linked alone and reads the address
    off the name."""
    return {n: int(m[1], 16) for n in names if (m := _AUTO_NAME.match(n))}


def linker_script(
    base: int,
    symbols: dict[str, list[Any]],
    originals: dict[str, int] | None = None,
    auto: dict[str, int] | None = None,
) -> str:
    """The link script: `originals` pins sibling functions to their original addresses (a script assignment
    overrides the object's own definition), every other known symbol is provided at its address."""
    pinned = "\n".join(f"{name} = 0x{vram:08X};" for name, vram in (originals or {}).items())
    rows = (
        pinned
        + "\n"
        + "\n".join(f"PROVIDE({name} = 0x{row[0]:08X});" for name, row in symbols.items() if row[0] is not None)
        + "\n"
        + "\n".join(f"PROVIDE({name} = 0x{vram:08X});" for name, vram in (auto or {}).items() if name not in symbols)
    )
    return f"""{rows}
SECTIONS {{
  . = 0x{base:08X};
  .text : {{ *(.text) *(.text.*) }}
  . = ALIGN(16);
  .rodata : {{ *(.rodata) *(.rodata.*) *(.rodata1) *(.rdata) *(.rdata.*) *(.lit4) *(.lit8) }}
  . = ALIGN(16);
  .data : {{ *(.data) *(.data.*) *(.sdata) *(.sdata.*) }}
  . = ALIGN(16);
  .bss : {{ *(.bss) *(.sbss) *(.scommon) *(COMMON) }}
  /DISCARD/ : {{ *(.reginfo) *(.MIPS.abiflags) *(.gnu.attributes) *(.mdebug*) *(.pdr) *(.comment) *(.options)
    *(.gptab*) }}
}}
"""


_SYMBOL = re.compile(r"^([0-9a-f]{8})\s+(\S+)\s+(?:(\S)\s+)?(\S+)\s+([0-9a-f]{8})\s+(\S+)$")


def function_table(objdump: str, elf: Path, text_end: int, base: int) -> list[dict[str, Any]]:
    """The functions of a linked ELF (symbols of .text), each with its offset from `base` and size."""
    out = subprocess.run([objdump, "-t", str(elf)], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        match = _SYMBOL.match(line.strip())
        if not match or match.group(4) != ".text" or match.group(3) != "F" or match.group(6).startswith(("$", ".")):
            continue
        rows.append((int(match.group(1), 16), int(match.group(5), 16), match.group(6)))
    rows = sorted(set(rows))
    table = []
    for index, (address, size, name) in enumerate(rows):
        following = rows[index + 1][0] if index + 1 < len(rows) else text_end
        size = size or following - address
        if size > 0 and address % 4 == 0:
            table.append({"name": name, "offset": address - base, "size": size - size % 4})
    return table


def text_functions(objdump: str, obj: Path) -> list[tuple[str, int, int]]:
    """(name, offset, size) of each function an object defines in .text, in symbol table order."""
    out = subprocess.run([objdump, "-t", str(obj)], capture_output=True, text=True, check=True).stdout
    found = []
    for line in out.splitlines():
        match = _SYMBOL.match(line.strip())
        if match and match.group(4) == ".text" and match.group(3) == "F" and not match.group(6).startswith(("$", ".")):
            found.append((match.group(6), int(match.group(1), 16), int(match.group(5), 16)))
    return found


def text_symbols(objdump: str, obj: Path) -> list[str]:
    """Names of the symbols an object defines in .text."""
    return [name for name, _, _ in text_functions(objdump, obj)]


_RELOCATION = re.compile(r"^([0-9a-f]{8})\s+(R_\w+)\s+([^\s+-]+)")


def relocations(objdump: str, obj: Path) -> list[tuple[int, str, str]]:
    """(offset, type, symbol) of each relocation of an object's .text."""
    out = subprocess.run([objdump, "-r", "-j", ".text", str(obj)], capture_output=True, text=True, check=False).stdout
    return [(int(m[1], 16), m[2], m[3]) for line in out.splitlines() if (m := _RELOCATION.match(line.strip()))]


def pc_targets(relocs: list[tuple[int, str, str]]) -> set[str]:
    """Symbols the code branches to PC-relative (`b`, `beq`, `bal`: R_MIPS_PC16 and kin). Such a branch reaches only
    +-128 KiB, so its target cannot be pinned to an original address far from the image; the object's own copy runs."""
    return {symbol for _, kind, symbol in relocs if kind.startswith("R_MIPS_PC")}


def referencing(
    functions: list[tuple[str, int, int]], relocs: list[tuple[int, str, str]], wanted: set[str]
) -> list[str]:
    """The object's functions whose code holds a relocation against one of `wanted`, in address order."""
    hits = {offset for offset, _, symbol in relocs if symbol in wanted}
    return [
        name
        for name, start, size in sorted(functions, key=lambda row: row[1])
        if any(start <= offset < start + size for offset in hits)
    ]


_UNDEFINED = re.compile(r"\*UND\*\s+[0-9a-f]{8}\s+(\S+)$")


def undefined_symbols(objdump: str, obj: Path) -> list[str]:
    """Names an object references without defining."""
    out = subprocess.run([objdump, "-t", str(obj)], capture_output=True, text=True, check=True).stdout
    matches = (_UNDEFINED.search(line.strip()) for line in out.splitlines())
    return [m[1] for m in matches if m and not m[1].startswith(("$", "."))]


def version_names(
    profile: Profile,
    *,
    version: str,
    functions: list[str],
    wanted: list[str],
    symbols: dict[str, list[Any]],
    project: Path,
    host: Path | None,
) -> dict[str, int]:
    """The addresses, in `version`, of names this version's tables lack but another version's code spells: the
    assembly of each function that uses them is read side by side in every version (see names.counterparts). The
    first function that resolves a name decides it."""
    tables = all_symbol_tables(profile, project, host)
    found: dict[str, int] = {}
    for function in functions:
        left = [n for n in wanted if n not in found]
        if not left:
            break
        found = {**_function_names(profile, tables, version, function, left, symbols, project, host), **found}
    return found


def _function_names(
    profile: Profile,
    tables: dict[str, dict[str, list[Any]]],
    version: str,
    function: str,
    wanted: list[str],
    symbols: dict[str, list[Any]],
    project: Path,
    host: Path | None,
) -> dict[str, int]:
    def listing(v: str) -> str | None:
        if function not in tables.get(v, {}):
            return None
        try:
            path = api_call(profile, "asm_path", [v, function], project=project, host=host)
            return (project / path).read_text(errors="replace")
        except (DecompError, OSError):
            return None

    mine = listing(version)
    if mine is None:
        return {}
    others = {v: text for v in tables if v != version and (text := listing(v)) is not None}
    return names.counterparts(mine, others, wanted, symbols)


def sections(objdump: str, elf: Path) -> dict[str, tuple[int, int]]:
    out = subprocess.run([objdump, "-h", str(elf)], capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].startswith(".") and re.fullmatch(r"[0-9a-f]{8}", parts[2]):
            found[parts[1]] = (int(parts[3], 16), int(parts[2], 16))
    return found


_SITE = re.compile(r"\S+\.i:(\d+)")


def real_sites(message: str, lines: list[list[Any]]) -> str:
    """Compiler diagnostics name lines of the preprocessed unit (`candidate.o.i:172`); `lines` (build context) maps
    each of its lines to the [file, line] it was written on, so both sites of a conflict can be opened."""

    def site(match: re.Match[str]) -> str:
        number = int(match[1])
        if not 1 <= number <= len(lines):
            return match[0]
        path, line = lines[number - 1]
        return f"{path}:{line}"

    return _SITE.sub(site, message)


_HEADING = re.compile(r": (In function `[^`]*'|At top level):\s*$|^\s*In file included from")


def error_lines(text: str, count: int = 4) -> list[str]:
    """The first `count` compiler errors of `text`. The causes come first and warnings follow them, so the last
    lines of a failed compile are mostly warnings; with no error line left, the last lines stand in."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "warning:" not in ln and not _HEADING.search(ln)]
    return errors[:count] or lines[-3:]


def build(
    profile: Profile,
    *,
    version: str,
    function: str,
    source: Path,
    work: Path,
    base: int,
    project: Path | None,
    host: Path | None,
    defines: tuple[str, ...] = ("NON_MATCHING",),
) -> Image:
    """Compile and link `source` for `function` in `version`; the image lands in `work`."""
    chosen = settings(profile, project, host)
    tools = host_tools(chosen.host)
    text = "".join(f"#define {d} 1\n" for d in defines) + source.read_text(errors="replace")
    context = api_call(profile, "build_context", [function, version, text], project=project, host=host)
    work.mkdir(parents=True, exist_ok=True)
    unit = work / "candidate.c"
    unit.write_text(context["source"])  # preprocessed as the build's compiler reads it, includes resolved
    script = work / "compile.sh"
    script.write_text(context["script"])
    obj = work / "candidate.o"
    # INCLUDE_ASM stand-ins in the file name assembly relative to the version's assembly directory (the parent of
    # `nonmatchings` in the build context's asm_path), as the build's own include path does.
    parts = Path(context.get("asm_path", "")).parts
    home = chosen.project.joinpath(*parts[: parts.index("nonmatchings")]) if "nonmatchings" in parts else chosen.project
    done = subprocess.run(
        ["sh", str(script), str(unit), str(work / "candidate.i"), str(obj)],
        cwd=home if home.is_dir() else chosen.project,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0 or not obj.is_file():
        shown = real_sites(" | ".join(error_lines(done.stderr + done.stdout)), context.get("lines", []))
        raise CandidateError("behave.compile", f"the candidate does not compile: {shown}")
    symbols = symbol_table(profile, version, project, host)
    defined = text_functions(tools["mips_objdump"], obj)
    relocs = relocations(tools["mips_objdump"], obj)
    # Sibling functions of the file that have an original address stay the original code: calls to them leave
    # the candidate. Only the function under test (and helpers with no original symbol) run from the image, and
    # siblings a PC-relative branch targets, which cannot leave the image.
    near = pc_targets(relocs)
    owned = [name for name, _, _ in defined if name != function and name in symbols and symbols[name][0] is not None]
    siblings = {name: symbols[name][0] for name in owned if name not in near}
    kept = tuple(name for name in owned if name in near)
    auto = auto_symbols(chosen.project, version)
    # Names of another version's vocabulary that this version's tables lack resolve through the assembly, in both
    # versions, of the functions that use them (the function under test first, then siblings), as the build learns
    # them.
    lacking = [
        n for n in undefined_symbols(tools["mips_objdump"], obj) if n not in symbols and n not in auto and n != function
    ]
    auto = {**named_addresses(lacking), **auto}
    lacking = [n for n in lacking if n not in auto]
    if lacking:
        auto = {
            **auto,
            **version_names(
                profile,
                version=version,
                functions=[function, *(n for n in referencing(defined, relocs, set(lacking)) if n != function)],
                wanted=lacking,
                symbols=symbols,
                project=chosen.project,
                host=host,
            ),
        }
    (work / "link.ld").write_text(linker_script(base, symbols, siblings, auto))
    elf = work / "candidate.elf"
    link = subprocess.run(
        [tools["mips_ld"], "-T", str(work / "link.ld"), "-o", str(elf), str(obj)],
        capture_output=True,
        text=True,
        check=False,
    )
    if link.returncode != 0:
        tail = [ln for ln in link.stderr.splitlines() if ln.strip()][-3:]
        raise CandidateError("behave.link", f"the candidate does not link at {base:#010x}: {' | '.join(tail)}")
    flat = work / "candidate.bin"
    # the assembler leaves .rdata without ALLOC, and objcopy writes only allocated sections
    marked = [f"--set-section-flags={name}=alloc,load,contents,readonly,data" for name in (".rodata", ".data")]
    wanted = [flag for name in (".text", ".rodata", ".data") for flag in ("-j", name)]
    subprocess.run(
        [tools["mips_objcopy"], *marked, "-O", "binary", *wanted, str(elf), str(flat)], check=True, capture_output=True
    )
    found = sections(tools["mips_objdump"], elf)
    text_size = found.get(".text", (0, 0))[1]
    table = function_table(tools["mips_objdump"], elf, base + text_size, base)
    if function not in {row["name"] for row in table}:
        raise CandidateError("behave.function", f"the candidate object defines no function {function}")
    size = flat.stat().st_size
    return Image(flat, base, tuple(table), size, hashlib.sha256(obj.read_bytes()).hexdigest(), kept)
