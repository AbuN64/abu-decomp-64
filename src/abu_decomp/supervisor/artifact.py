"""Stage and display one real image result without manufacturing a graphic."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abu_decomp.files import atomic_json, run_logged, sha256_file


def show(profile: Any, config: dict[str, Any], source: Path, name: str) -> dict[str, Any]:
    source = source.resolve(strict=True)
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg"} or not source.is_file():
        raise ValueError("artifact must be an existing PNG/JPEG")
    root = profile.resolve_path(config["artifact_root"])
    metadata_path = profile.resolve_path(config["metadata_path"])
    root.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:60] or "result"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / (slug + "-" + timestamp + source.suffix.lower())
    fd, temporary = tempfile.mkstemp(prefix=".image-", dir=root)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    record = {
        "schema": 1,
        "source": str(source),
        "display_copy": str(destination),
        "name": name,
        "sha256": sha256_file(destination),
        "label": str(config.get("result_label", "last actual result")),
    }
    atomic_json(metadata_path, record)
    command = config.get("restart_command")
    if command:
        run_logged(
            [str(item) for item in command],
            stage="artifact viewer restart",
            failure_key="panes.artifact_restart_failed",
        )
    return {"artifact": record, "metadata_path": str(metadata_path)}


def current(profile: Any, config: dict[str, Any]) -> dict[str, Any]:
    metadata_path = profile.resolve_path(config["metadata_path"])
    record = json.loads(metadata_path.read_text())
    image = Path(record["display_copy"]).resolve(strict=True)
    if not image.is_relative_to(profile.resolve_path(config["artifact_root"]).resolve()):
        raise ValueError("artifact path is outside configured root")
    if sha256_file(image) != record["sha256"]:
        raise ValueError("artifact image hash changed after staging")
    return record
