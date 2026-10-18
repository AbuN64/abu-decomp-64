"""One immutable release/current mechanism for every configured role."""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from abu_decomp.files import atomic_json, sha256_file

from .util import BuildFailure, expand, run_gate, safe_relative


def prepare(specs, variables, resolve_path):
    """Stage and verify every release before any current pointer changes."""
    prepared = []
    for spec in specs:
        name = spec["name"]
        root = resolve_path(expand(spec["root"], variables))
        revision = expand(spec["revision"], variables)
        if not revision or revision in (".", "..") or "/" in revision or "\\" in revision:
            raise BuildFailure(f"Unsafe release revision for {name}: {revision!r}")
        root.mkdir(parents=True, exist_ok=True)
        destination = root / revision
        files = {}
        sources = {}
        for item in spec["files"]:
            source_key = item["source"]
            if source_key not in variables or not source_key.startswith(("input.", "output.")):
                raise BuildFailure(f"Unknown release input {source_key!r} for {name}")
            source = Path(variables[source_key])
            target = safe_relative(item["target"])
            if str(target) in files:
                raise BuildFailure(f"Duplicate release path: {name}/{target}")
            if not source.is_file() or source.is_symlink():
                raise BuildFailure(f"Release source is missing or a symlink: {source}")
            sources[str(target)] = source
            files[str(target)] = {
                "sha256": sha256_file(source),
                "bytes": source.stat().st_size,
                "mode": item.get("mode"),
            }
        manifest = {"name": name, "revision": revision, "files": files}
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise BuildFailure(f"Existing release path is not an immutable directory: {destination}")
            recorded = json.loads((destination / "manifest.json").read_text())
            if recorded != manifest:
                raise BuildFailure(f"Existing release {destination} differs from proved file map")
        else:
            staging = Path(tempfile.mkdtemp(prefix=f".stage-{revision}.", dir=root))
            try:
                for relative, details in files.items():
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sources[relative], target, follow_symlinks=False)
                    if details["mode"] is not None:
                        target.chmod(details["mode"])
                    if sha256_file(target) != details["sha256"]:
                        raise BuildFailure(f"Release copy differs from proved input: {target}")
                atomic_json(staging / "manifest.json", manifest)
                os.rename(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        for relative, details in files.items():
            target = destination / relative
            if not target.is_file() or target.is_symlink() or sha256_file(target) != details["sha256"]:
                raise BuildFailure(f"Release readback failed: {target}")
        actual = {str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()}
        if actual != set(files) | {"manifest.json"}:
            raise BuildFailure(f"Release contains unreviewed files: {destination}")
        prepared.append(
            {
                "name": name,
                "root": root,
                "revision": revision,
                "destination": destination,
                "manifest": manifest,
                "activate": spec.get("activate", []),
                "rollback": spec.get("rollback", []),
            }
        )
    return prepared


def _switch(current, target):
    temporary = current.parent / f".current.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target.name)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def promote(prepared, variables, log_dir, on_step, ensure_current):
    """Switch all roles, activate, verify, and restore prior currents on error."""
    previous = {}
    switched = []
    for item in prepared:
        current = item["root"] / "current"
        if current.exists() or current.is_symlink():
            if not current.is_symlink():
                raise BuildFailure(f"Current pointer is not a symlink: {current}")
            prior = current.resolve(strict=True)
            if prior.parent != item["root"]:
                raise BuildFailure(f"Current release escapes its role root: {current}")
            previous[item["name"]] = prior
        else:
            previous[item["name"]] = None
    steps = []
    try:
        ensure_current("pre")
        for item in prepared:
            current = item["root"] / "current"
            _switch(current, item["destination"])
            switched.append(item)
        for item in prepared:
            release_values = {
                **variables,
                "release": str(item["destination"]),
                "release_current": str(item["root"] / "current"),
                "release_revision": item["revision"],
            }
            for index, operation in enumerate(item["activate"]):
                spec = {**operation, "name": f"activate-{item['name']}-{index}"}
                try:
                    step = run_gate(
                        spec, release_values, item["root"], Path(log_dir) / f"{spec['name']}.log", on_start=on_step
                    )
                    steps.append(step)
                except BuildFailure as error:
                    if error.step:
                        steps.append(error.step)
                    raise
            if (item["root"] / "current").resolve(strict=True) != item["destination"]:
                raise BuildFailure(f"Current release changed unexpectedly: {item['name']}")
            for relative, details in item["manifest"]["files"].items():
                if sha256_file(item["destination"] / relative) != details["sha256"]:
                    raise BuildFailure(f"Activated release readback changed: {item['name']}/{relative}")
            ensure_current("post")
    except (BuildFailure, OSError) as failure:
        rollback_errors = []
        for item in reversed(switched):
            current = item["root"] / "current"
            if current.resolve() != item["destination"]:
                rollback_errors.append(f"{item['name']}: newer external current retained")
                continue
            prior = previous[item["name"]]
            if prior is None:
                current.unlink(missing_ok=True)
            else:
                _switch(current, prior)
            rollback_values = {
                **variables,
                "release": str(prior) if prior else "",
                "release_current": str(current),
                "release_revision": prior.name if prior else "",
            }
            for index, operation in enumerate(item["rollback"]):
                spec = {**operation, "name": f"rollback-{item['name']}-{index}"}
                try:
                    steps.append(
                        run_gate(
                            spec, rollback_values, item["root"], Path(log_dir) / f"{spec['name']}.log", on_start=on_step
                        )
                    )
                except BuildFailure as error:
                    if error.step:
                        steps.append(error.step)
                    rollback_errors.append(str(error))
        raise BuildFailure(f"{failure}; rollback: {rollback_errors}", steps=steps) from failure
    return {
        "releases": [
            {
                "name": item["name"],
                "current": str(item["root"] / "current"),
                "destination": str(item["destination"]),
                "manifest": item["manifest"],
            }
            for item in prepared
        ],
        "steps": steps,
    }
