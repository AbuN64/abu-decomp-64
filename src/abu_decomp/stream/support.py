"""The two small helpers the stream commands need, so the stream never imports another tool's internals."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from abu_decomp import DecompError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
