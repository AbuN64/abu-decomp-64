"""Check stamps: a diff that touches declarations or data is queued only after `decomp data check` passed on it.

`data check --diff D` writes STAMPS/<sha256 of D>.ok when every version of the config built and no exact function
stopped matching; `data stamp --diff D` (the queue's append) and `data gate --stamps` (the train) refuse a touching diff
without one. A diff that touches no declaration and names no data symbol needs none."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from abu_decomp.data.config import DataConfig

HEADER = re.compile(r"\.h$", re.I)


def files_of(text: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", text, re.M)))


def touches_data(cfg: DataConfig, text: str) -> bool:
    """A header changed, an `extern` line changed, or a changed line names a data symbol."""
    if any(HEADER.search(f) for f in files_of(text)):
        return True
    token = re.compile(r"\b" + cfg.symbol_token)
    for line in text.splitlines():
        if (
            line[:1] in "+-"
            and line[:3] not in ("+++", "---")
            and (re.search(r"\bextern\b", line) or token.search(line))
        ):
            return True
    return False


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def stamp_dir(given: Path | None, config: Path | None) -> Path:
    if given:
        return given
    if os.environ.get("DECOMP_DATA_STAMPS"):
        return Path(os.environ["DECOMP_DATA_STAMPS"])
    return (config.expanduser().parent if config else Path.cwd()) / "stamps"


def stamped(directory: Path, text: str) -> bool:
    return (directory / f"{digest(text)}.ok").is_file()


def write(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest(text)}.ok"
    path.write_text("\n".join(files_of(text)) + "\n")
    return path


def missing(cfg: DataConfig, directory: Path, text: str, diff: Path) -> list[str]:
    """Violations: the diff touches declarations or data and no passing all-version check stamp exists for its hash."""
    if not touches_data(cfg, text) or stamped(directory, text):
        return []
    return [
        f"{diff.name} touches declarations or data and has no passing all-version `decomp data check` stamp "
        f"(sha256 {digest(text)[:12]}): run `decomp data check --tree TREE --diff {diff}` on the tree it came from"
    ]
