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


def linker_script(base: int, symbols: dict[str, list[Any]], originals: dict[str, int] | None = None) -> str:
    """The link script: `originals` pins sibling functions to their original addresses (a script assignment
    overrides the object's own definition), every other known symbol is provided at its address."""
    pinned = "\n".join(f"{name} = 0x{vram:08X};" for name, vram in (originals or {}).items())
    rows = (
        pinned
        + "\n"
        + "\n".join(f"PROVIDE({name} = 0x{row[0]:08X});" for name, row in symbols.items() if row[0] is not None)
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


def text_symbols(objdump: str, obj: Path) -> list[str]:
    """Names of the symbols an object defines in .text."""
    out = subprocess.run([objdump, "-t", str(obj)], capture_output=True, text=True, check=True).stdout
    names = []
    for line in out.splitlines():
        match = _SYMBOL.match(line.strip())
        if match and match.group(4) == ".text" and match.group(3) == "F" and not match.group(6).startswith(("$", ".")):
            names.append(match.group(6))
    return names


def sections(objdump: str, elf: Path) -> dict[str, tuple[int, int]]:
    out = subprocess.run([objdump, "-h", str(elf)], capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].startswith(".") and re.fullmatch(r"[0-9a-f]{8}", parts[2]):
            found[parts[1]] = (int(parts[3], 16), int(parts[2], 16))
    return found


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
        tail = [ln for ln in (done.stderr + done.stdout).splitlines() if ln.strip()][-3:]
        raise CandidateError("behave.compile", f"the candidate does not compile: {' | '.join(tail)}")
    symbols = symbol_table(profile, version, project, host)
    # Sibling functions of the file that have an original address stay the original code: calls to them leave
    # the candidate. Only the function under test (and helpers with no original symbol) run from the image.
    siblings = {
        name: symbols[name][0]
        for name in text_symbols(tools["mips_objdump"], obj)
        if name != function and name in symbols and symbols[name][0] is not None
    }
    (work / "link.ld").write_text(linker_script(base, symbols, siblings))
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
    return Image(flat, base, tuple(table), size, hashlib.sha256(obj.read_bytes()).hexdigest())
