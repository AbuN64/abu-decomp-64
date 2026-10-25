"""Project facts for `decomp data`: the tool names no game, a TOML file does.

The file is `--config FILE`, else `$DECOMP_DATA_CONFIG`, else `<tree>/docs/data.toml`. Every key has a default that
carries no game fact except the symbol shape, so a project with another naming only changes this file:

    [data]
    reference = "ver-a"                       # the version whose addresses name clusters and queue rows
    versions = ["ver-a", "ver-b"]
    symbol = '^D_(auto_)?[0-9A-F]{8}(_\\w+)?$'  # a raw (unnamed) data symbol
    data_prefix = "D_"                        # every symbol with this prefix is a data label
    symbol_token = 'D_(?:auto_)?[0-9A-Fa-f]{8}\\w*'
    symbols_file = "versions/{v}/symbol_addrs.txt"
    data_asm = "asm/{v}/data"
    fn_asm = "asm/{v}/nonmatchings"
    map_file = "docs/map-{v}.json"            # {"fns": [[name, ..., score, unit], ...]}
    map_columns = {name = 0, score = 3, unit = 4}
    source_dirs = ["src", "include"]
    source_suffixes = [".c", ".cpp", ".h"]
    prelude = "TYPES.H"                       # included first by a synthetic probe file
    version_define = "VERSION_{V}"            # {V}: version upper-cased, '-' as '_'
    cpp = ["cpp", "-P", "-undef", "-nostdinc", "-D_LANGUAGE_C", "-Iinclude"]
    cc1 = "~/.local/share/unbake/toolchains/gcc-2.8.1-sn64/cc1"
    cc1_flags = ["-quiet", "-G0", "-O2"]
    make = ["make"]                           # prefix of a build: `unbake-run make`
    make_env = {}
    jobs = ["~/.config/m2/admit", "1.5"]      # prints the -j count
    rom_check = ["check", "CHECK=rom"]        # arguments after `make -jN`: link every version and compare its ROM hash
    rom_image = "build/{v}/RageWars.z64"      # the linked ROM of a version, and the original it must equal (for the report)
    rom_base = "roms/baserom.{v}.z64"
    object = "build/{v}/{unit}.o"
    fncmp = ["python3", "fncmp.py", "--aligned", "{v}", "{fn}", "{obj}"]
    fncmp_score = 'edit=(\\d+)'                # group 1: the edit count (0 = exact)
    fncmp_batch = ["python3", "fncmp.py", "--aligned", "--batch", "{v}"]  # optional: reads "FN OBJ" lines, prints "FN <result line>"
    census = "~/census.json"                  # optional: the gate's census; stamp/check refuse a gate violation before building
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abu_decomp import DecompError

ENV = "DECOMP_DATA_CONFIG"


def user_config() -> Path:
    """The host's own data config (a rig links it to its shared file), read when nothing else names one."""
    return Path.home() / ".config" / "abu-decomp" / "data.toml"


@dataclass(frozen=True)
class DataConfig:
    reference: str = "ref"
    versions: tuple[str, ...] = ("ref",)
    symbol: str = r"^D_(auto_)?[0-9A-F]{8}(_\w+)?$"
    data_prefix: str = "D_"
    symbol_token: str = r"D_(?:auto_)?[0-9A-Fa-f]{8}\w*"
    symbols_file: str = "versions/{v}/symbol_addrs.txt"
    data_asm: str = "asm/{v}/data"
    fn_asm: str = "asm/{v}/nonmatchings"
    map_file: str = "docs/map-{v}.json"
    map_columns: dict[str, int] = field(default_factory=lambda: {"name": 0, "score": 3, "unit": 4})
    source_dirs: tuple[str, ...] = ("src", "include")
    source_suffixes: tuple[str, ...] = (".c", ".cpp", ".h")
    prelude: str = ""
    version_define: str = "VERSION_{V}"
    cpp: tuple[str, ...] = ("cpp", "-P", "-undef", "-nostdinc", "-D_LANGUAGE_C", "-Iinclude")
    cc1: str = ""
    cc1_flags: tuple[str, ...] = ("-quiet", "-G0", "-O2")
    make: tuple[str, ...] = ("make",)
    make_env: dict[str, str] = field(default_factory=dict)
    jobs: tuple[str, ...] = ()
    rom_check: tuple[str, ...] = ()
    rom_image: str = ""
    rom_base: str = ""
    object: str = "build/{v}/{unit}.o"
    fncmp: tuple[str, ...] = ()
    fncmp_score: str = r"edit=(\d+)"
    fncmp_batch: tuple[
        str, ...
    ] = ()  # one process for every function of a version (a start + objdump per call otherwise)
    census: str = (
        ""  # census.json the gate reads: stamp --build and check --diff refuse what the gate would, before any build
    )
    evidence_tree: str = ""  # a tree whose asm/<v>/ the gate reads to tell a separate global from a struct member

    def define(self, version: str) -> str:
        return "-D" + self.version_define.format(V=version.upper().replace("-", "_"))

    def path(self, template: str, version: str) -> str:
        return template.format(v=version)


_TUPLES = {
    "versions",
    "source_dirs",
    "source_suffixes",
    "cpp",
    "cc1_flags",
    "make",
    "jobs",
    "fncmp",
    "fncmp_batch",
    "rom_check",
}
_KEYS = {f for f in DataConfig.__dataclass_fields__}


def parse(table: dict[str, Any], source: str = "config") -> DataConfig:
    unknown = sorted(set(table) - _KEYS)
    if unknown:
        # A key this tool does not know yet is ignored with a warning, never a refusal: a config deployed before its tool
        # (10-24: rom_check live in rw.toml first) made every `data` verb, and so claim.sh, refuse for ~15 min.
        print(
            f"decomp data: {source}: ignoring unknown key(s) {', '.join(unknown)} (this tool predates them)",
            file=sys.stderr,
        )
    values = {k: (tuple(v) if k in _TUPLES else v) for k, v in table.items() if k in _KEYS}
    cfg = DataConfig(**values)
    if cfg.reference not in cfg.versions and cfg.versions != ("ref",):
        raise DecompError("data.config", f"{source}: reference {cfg.reference!r} is not in versions")
    return cfg


def load(path: Path | None, tree: Path | None = None) -> DataConfig:
    """The config file named by --config, $DECOMP_DATA_CONFIG or <tree>/docs/data.toml; refuses when none exists."""
    candidates = [path] if path else []
    if os.environ.get(ENV):
        candidates.append(Path(os.environ[ENV]))
    if tree:
        candidates.append(tree / "docs" / "data.toml")
    candidates.append(user_config())
    for c in candidates:
        if c and c.expanduser().is_file():
            try:
                text = tomllib.loads(c.expanduser().read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise DecompError("data.config", f"{c}: {exc}") from exc
            return parse(text.get("data", text), str(c))
    raise DecompError(
        "data.config",
        "no data config: pass --config FILE, set DECOMP_DATA_CONFIG or add <tree>/docs/data.toml",
        next="decomp data census --config PATH --tree TREE --out DIR",
    )


def expand(value: str) -> str:
    return os.path.expanduser(value)
