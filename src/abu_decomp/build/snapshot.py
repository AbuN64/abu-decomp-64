"""Maintain one private, stable source tree from a pinned Git commit archive."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from abu_decomp.files import atomic_json, sha256_bytes, sha256_file


def verify_snapshot(source_root, manifest, protected_top=()):
    """Reject changes to archived files and unowned files outside build roots."""
    root = Path(source_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Snapshot source root is missing or a symlink: {root}")
    for name in protected_top:
        if not name or name in (".", "..", ".git") or "/" in name or "\\" in name:
            raise ValueError(f"Invalid protected snapshot root: {name!r}")
        protected = root / name
        if protected.exists() and (protected.is_symlink() or not protected.is_dir()):
            raise ValueError(f"Protected snapshot root is not a directory: {protected}")
    expected = manifest["files"]
    seen = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        relative_parent = parent.relative_to(root)
        if relative_parent == Path("."):
            dirs[:] = [name for name in dirs if name not in protected_top]
        for name in dirs:
            path = parent / name
            if path.is_symlink():
                raise ValueError(f"Snapshot contains an unexpected symlink: {path}")
        for name in names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file() or relative not in expected:
                raise ValueError(f"Snapshot contains an unexpected file or symlink: {path}")
            item = expected[relative]
            if sha256_file(path) != item["sha256"] or (path.stat().st_mode & 0o777) != item["mode"]:
                raise ValueError(f"Snapshot content or mode differs from committed archive: {path}")
            seen.add(relative)
    missing = set(expected) - seen
    if missing:
        raise ValueError(f"Snapshot is missing committed files: {sorted(missing)}")


def safe_member_path(name, protected_top=()):
    value = PurePosixPath(name)
    if value.is_absolute() or any(part in ("", ".", "..") for part in value.parts):
        raise ValueError(f"Unsafe archive path: {name!r}")
    if value.parts[0] == ".git" or value.parts[0] in protected_top:
        raise ValueError(f"Archive path overlaps private metadata/build: {name!r}")
    return value


def sync_snapshot(writer_repo, commit, source_root, manifest_path, protected_top=()):
    """Copy only changed committed files; preserve unchanged file mtimes.

    Files absent from a later commit are removed only if the previous manifest
    owned them and their content still matches it. Build outputs are unowned.
    A repeated call recovers interrupted syncs when a file already matches the
    requested commit, without accepting unrelated edits to the private tree.
    """
    writer_repo = Path(writer_repo)
    source_root = Path(source_root)
    manifest_path = Path(manifest_path)
    source_root.mkdir(parents=True, exist_ok=True)
    source_real = source_root.resolve()
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": {}}
    prior_files = previous["files"]
    archive = subprocess.run(
        ["git", "-C", str(writer_repo), "archive", "--format=tar", commit], check=True, capture_output=True
    ).stdout
    archive_hash = sha256_bytes(archive)
    desired = {}
    directories = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = safe_member_path(member.name, protected_top)
            if member.isdir():
                directories.add(str(name))
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported archive entry: {member.name}")
            data = tar.extractfile(member).read()
            desired[str(name)] = {"sha256": sha256_bytes(data), "mode": member.mode & 0o777, "data": data}
            directories.update(str(parent) for parent in name.parents if str(parent) != ".")

    removed = 0
    for name, old in prior_files.items():
        if name in desired:
            continue
        path = source_root / name
        if path.exists():
            if path.is_symlink() or not path.is_file() or sha256_file(path) != old["sha256"]:
                raise ValueError(f"Prior snapshot file was modified outside sync: {path}")
            path.unlink()
            removed += 1

    for name in sorted(directories, key=lambda value: (value.count("/"), value)):
        path = source_root / name
        if not path.resolve(strict=False).is_relative_to(source_real):
            raise ValueError(f"Private source path escapes tree: {path}")
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise ValueError(f"Private source directory conflicts with existing file: {path}")
        path.mkdir(exist_ok=True)

    changed = unchanged = 0
    for name, item in desired.items():
        path = source_root / name
        if not path.resolve(strict=False).is_relative_to(source_real):
            raise ValueError(f"Private source path escapes tree: {path}")
        old = prior_files.get(name)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Private source file conflicts with existing path: {path}")
            current_hash = sha256_file(path)
            if current_hash == item["sha256"]:
                if (path.stat().st_mode & 0o777) != item["mode"]:
                    path.chmod(item["mode"])
                unchanged += 1
                continue
            if old is None or current_hash != old["sha256"]:
                raise ValueError(f"Private source file was modified outside sync: {path}")
            prior_mtime = path.stat().st_mtime_ns
        else:
            prior_mtime = 0
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(item["data"])
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, item["mode"])
            now = max(time.time_ns(), prior_mtime + 1)
            os.utime(temporary, ns=(now, now))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed += 1

    files = {name: {key: value for key, value in item.items() if key != "data"} for name, item in desired.items()}
    manifest = {"commit": commit, "archive_sha256": archive_hash, "files": files}
    atomic_json(manifest_path, manifest, sort_keys=True, ensure_ascii=True)
    return {
        "commit": commit,
        "archive_sha256": archive_hash,
        "files": len(files),
        "changed": changed,
        "unchanged": unchanged,
        "removed": removed,
    }
