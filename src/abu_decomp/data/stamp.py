"""Check stamps: a diff that touches declarations or data is queued only after `decomp data check` passed on it.

`data check --diff D` writes STAMPS/<sha256 of D>.ok when every version of the config built and no exact function
stopped matching; `data stamp --diff D` (the queue's append) and `data gate --stamps` (the train) refuse a touching diff
without one. A diff that touches no declaration and names no data symbol needs none."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from abu_decomp.data import config as config_module
from abu_decomp.data.config import DataConfig

HEADER = re.compile(r"\.h$", re.I)


def files_of(text: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", text, re.M)))


_FACT = re.compile(r"^[+-]\s*(\w+)\s*=\s*(0[xX][0-9A-Fa-f]+)\s*;")


def renames_of(text: str) -> dict[str, str]:
    """old name -> new name for every symbol row of the diff that keeps its address and changes its name."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    for line in text.splitlines():
        m = _FACT.match(line) if line[:3] not in ("+++", "---") else None
        if m:
            (old if line[0] == "-" else new)[m.group(2).lower()] = m.group(1)
    return {old[a]: new[a] for a in old if a in new and old[a] != new[a]}


_COMMENT = re.compile(r"/\*.*?\*/|//.*$")


def pure_rename(text: str, code: tuple[str, ...] = ()) -> bool:
    """The diff only renames symbols: it holds symbol rows that change a name at the same address, and every other changed
    line becomes its replacement when those names are substituted (comments aside); a file that is no source or header
    (a name table) may also gain rows. No type, size or value moves, so the diff changes no byte, `unbake renameproof` is
    its proof and the all-version data check (295 s for 74 files, 10-24) has nothing to judge."""
    names = renames_of(text)
    if not names:
        return False
    # Declarations a rename makes redundant go too (an `extern` the shared header now declares) and the include that brings
    # that header in: neither moves a type, a size or a value, and the all-ROM check still proves every byte.
    include = re.compile(r"^#\s*include\b")
    sub = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b")
    plain = lambda line: _COMMENT.sub("", line).strip()  # noqa: E731
    gone: list[str] = []
    came: list[str] = []
    current = ""

    def settled() -> bool:
        if not current.lower().endswith((*code, ".h")):
            # a name table drops the alias row of a symbol the diff renames (the new name sits in symbol_addrs: `literals_<fn>`)
            gone[:] = [g for g in gone if not ((m := _FACT.match("-" + g)) and m.group(1) in names)]
        table = not current.lower().endswith((*code, ".h")) and not gone
        old = [sub.sub(lambda m: names[m.group(1)], plain(g)) for g in gone]
        new = [plain(c) for c in came if not include.match(plain(c))]
        old = [g for g in old if not (g.startswith("extern ") and g not in new)]
        ok = table or old == new
        gone.clear()
        came.clear()
        return ok

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if (gone or came) and not settled():
                return False
            current = line.split(" b/", 1)[-1]
        elif line.startswith("-") and not line.startswith("---") and not came:
            gone.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            came.append(line[1:])
        elif (gone or came) and not settled():
            return False
    return settled() if (gone or came) else True


def touches_data(cfg: DataConfig, text: str) -> bool:
    """A header changed, or an `extern` line or a data symbol changed in a C source file (a Makefile or a note never
    declares data). A diff that only renames symbols (`pure_rename`) touches none."""
    if pure_rename(text, tuple(cfg.source_suffixes)):
        return False
    suffixes = tuple(cfg.source_suffixes)
    token = re.compile(r"\b" + cfg.symbol_token)
    current = ""
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            if HEADER.search(current):
                return True
        elif (
            current.lower().endswith(suffixes)
            and line[:1] in "+-"
            and line[:3] not in ("+++", "---")
            and (re.search(r"\bextern\b", line) or token.search(line))
        ):
            return True
    return False


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def stamp_dir(given: Path | None, config: Path | None) -> Path:
    """ONE resolution for stamp, check, gate and the queue: explicit --stamps, $DECOMP_DATA_STAMPS, the host config's
    stamps (a lane passing its own --config used to stamp into that copy's dir while the queue read the host's:
    CLEAN-COMMON-1 pack 4 was stamped, refused and waited on), then the given config's dir, then $DECOMP_DATA_CONFIG's."""
    if given:
        return given
    if os.environ.get("DECOMP_DATA_STAMPS"):
        return Path(os.environ["DECOMP_DATA_STAMPS"])
    if (user := config_module.user_config()).is_file():  # the host's own config: its stamps sit beside the real file
        return user.resolve().parent / "stamps"
    if config:
        return config.expanduser().parent / "stamps"
    if os.environ.get("DECOMP_DATA_CONFIG"):
        return Path(os.environ["DECOMP_DATA_CONFIG"]).expanduser().parent / "stamps"
    # never the current directory: a stamp run from a lane tree wrote stamps/ into it and `data check` then refused t...
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "decomp-data" / "stamps"


def body(text: str) -> str:
    """TEXT without its leading comment lines (the train's `# MSG:` subject, lane notes): adding or editing the subject after
    the check keeps the stamp (HONEST-O1 ran the data check twice after claim.sh asked for the MSG line)."""
    lines = text.splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    return "".join(lines)


def stamped(directory: Path, text: str) -> bool:
    return any((directory / f"{digest(t)}.ok").is_file() for t in (body(text), text))


def write(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest(body(text))}.ok"
    path.write_text("\n".join(files_of(text)) + "\n")
    return path


def missing(cfg: DataConfig, directory: Path, text: str, diff: Path) -> list[str]:
    """Violations: the diff touches declarations or data and no passing all-version check stamp exists for its hash."""
    if not touches_data(cfg, text) or stamped(directory, text):
        return []
    return [
        f"{diff.name} touches declarations or data and has no passing all-version `decomp data check` stamp "
        f"(sha256 {digest(body(text))[:12]}): run `decomp data check --tree TREE --diff {diff}` on the tree it came from"
    ]


EXTRACTION_INPUT = re.compile(r"(^|/)(symbol_addrs[^/]*\.txt|splat\.ya?ml)$")


def extraction_files(files: list[str]) -> list[str]:
    """The files of a change that the ROM extraction reads (symbol rows, splat config): a tree that takes them has stale asm
    until `unbake setup` runs there."""
    return [f for f in files if EXTRACTION_INPUT.search(f)]
