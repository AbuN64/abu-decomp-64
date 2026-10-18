"""Atomic writes, SHA-256 digests and finite child runs shared by build, supervisor and stream.

Moved 1:1 from cake_taste.files (the parts decomp uses) and cake_taste.logs.child.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from abu_decomp import DecompError


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write data beside the target, fsync, then rename over it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(
    path: str | os.PathLike[str], value: Any, *, sort_keys: bool = False, ensure_ascii: bool = False
) -> None:
    text = json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=ensure_ascii) + "\n"
    atomic_bytes(path, text.encode("utf-8"))


def run_logged(argv: list[str], *, stage: str, failure_key: str) -> None:
    """Keep finite child output off the one-envelope stdout channel."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)
    except OSError as error:
        raise DecompError(failure_key, f"{stage}: {error}") from error
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode:
        raise DecompError(failure_key, f"{stage} child exited {result.returncode}")
