"""Keep the oracle's generated facts current with the tree `decomp behave` runs on.

Cake Taste reads function boundaries from a generated symbols file (and the oracle host is built from the same
generated output), frozen when someone last ran `taste recomp generate`. A lane that moves a function's start
(`symbol_addrs`, layout) then met `behave.probe: no function starts at ...` until the generated output was rebuilt by
hand. Here every behave call digests the symbol and layout inputs of the tree it acts on; when they differ from what
the generated output was made from, the output (and the facts file) is regenerated from that tree, under a lock that
runs in-flight comparisons first. The oracle host's own stale check then rebuilds it from the new output."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, tools
from abu_decomp.config import Profile


class RekeyError(DecompError):
    pass


def inputs(project: Path, recomp: dict[str, Any], version: str) -> list[Path]:
    """The files the generated boundaries come from: the symbols file and the layout files of the version."""
    files = [project / str(recomp["symbols_file"]).format(version=version)]
    files += sorted(project.glob(str(recomp["layout_glob"]).format(version=version)))
    return files


def digest(project: Path, recomp: dict[str, Any], version: str) -> str:
    """The key of the generated output: the boundary inputs of PROJECT and the profile recomp facts themselves (an
    edit of local_indirect_jumps left the old output standing until `taste recomp generate` ran by hand, PF6 10-25)."""
    total = hashlib.sha256(json.dumps(recomp, sort_keys=True).encode())
    for path in inputs(project, recomp, version):
        try:
            body = path.read_bytes()
        except OSError:
            body = b"\0absent"
        total.update(f"{path.relative_to(project)}\0{len(body)}\0".encode() + body)
    return total.hexdigest()


def _under(value: Any, old: Path, new: Path) -> Any:
    if isinstance(value, str) and (value == str(old) or value.startswith(str(old) + "/")):
        return str(new) + value[len(str(old)) :]
    return value


def derive(data: dict[str, Any], project: Path) -> dict[str, Any]:
    """The Taste profile with the project paths (root, decomp root, ROMs) pointing at `project`."""
    old = Path(data["project_root"])
    out = json.loads(json.dumps(data))
    out["project_root"] = str(project)
    recomp = out.get("recomp", {})
    if "decomp_root" in recomp:
        recomp["decomp_root"] = str(project)
    recomp["roms"] = {k: _under(v, old, project) for k, v in recomp.get("roms", {}).items()}
    return out


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}")
    scratch.write_text(text)
    if mode is not None:
        scratch.chmod(mode)  # before the rename: the file is never visible with another mode
    scratch.replace(path)


def _facts_paths(base: Path, data: dict[str, Any]) -> list[Path]:
    repro = data.get("repro", {})
    seen: list[Path] = []
    for section in ("capture", "bounded"):
        value = repro.get(section, {}).get("facts")
        if value:
            path = Path(value).expanduser()
            path = (path if path.is_absolute() else base.parent / path).resolve()
            if path not in seen:
                seen.append(path)
    return seen


def _regenerate(profile: Profile, derived: Path, data: dict[str, Any], base: Path, version: str) -> None:
    with tools.taste_profile_override(derived):
        done = tools.invoke("taste", profile, ["recomp", "generate", "--version", version])
        if not done.ok:
            raise RekeyError("behave.rekey", _why(done, "taste recomp generate"))
        for path in _facts_paths(base, data):
            fresh = path.with_name(f".{path.name}.gen.{os.getpid()}")
            try:
                done = tools.invoke("taste", profile, ["recomp", "facts", "--version", version, "--output", str(fresh)])
                if not done.ok:
                    raise RekeyError("behave.rekey", _why(done, "taste recomp facts"))
                _merge_facts(path, json.loads(fresh.read_text()))
            finally:
                fresh.unlink(missing_ok=True)


def _recorded(path: Path) -> tuple[dict[str, Any], int]:
    """The facts file as the project recorded it ({} when there is none yet, and its mode). A file that does not parse
    is never treated as empty (10-24: that dropped boot/input/scenarios/cpu and behave refused for 5 min): the last good
    copy stands in, else the rewrite is refused."""
    last = path.with_name(f".{path.name}.last-good")
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return {}, 0o600
    for source in (path, last):
        try:
            old = json.loads(source.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(old, dict):
            return old, mode
    raise RekeyError("behave.rekey", f"{path} exists but is not a JSON object and has no last-good copy: not rewritten")


def _merge_facts(path: Path, generated: dict[str, Any]) -> None:
    """The facts file also holds what the project recorded by hand (boot state, input, scenarios, cpu): only the keys
    the generator makes are replaced. The merge is validated before it is written (no recorded key lost, none changed
    to another type by the generator, the generated keys present) and written atomically; the previous copy is kept as
    `.<name>.last-good`."""
    old, mode = _recorded(path)
    merged = {**old, **generated}
    lost = sorted(k for k in old if k not in merged)
    retyped = sorted(k for k in old if k in merged and k not in generated and type(merged[k]) is not type(old[k]))
    if not generated or lost or retyped:
        raise RekeyError(
            "behave.rekey",
            f"{path}: merged facts refused (generated {len(generated)} keys, lost {lost}, retyped {retyped})",
        )
    if old:
        _write(path.with_name(f".{path.name}.last-good"), json.dumps(old, indent=1, sort_keys=True) + "\n")
    _write(path, json.dumps(merged, indent=1, sort_keys=True) + "\n", mode)


def _why(done: Any, what: str) -> str:
    said = "; ".join(str(f.get("message") or f) for f in done.document.get("findings", [])) or str(done.returncode)
    return f"{what} failed: {said}"


@contextmanager
def rekeyed(profile: Profile, project: Path, version: str | None) -> Iterator[None]:
    """Run the body with Taste's generated output made from `project`'s boundaries, holding the lock shared."""
    base = tools.taste_profile_of(profile)
    if base is None or not version or not base.is_file():
        yield
        return
    data = json.loads(base.read_text())
    recomp = data.get("recomp") or {}
    if not {"gen_root", "symbols_file", "layout_glob"} <= set(recomp) or "project_root" not in data:
        yield
        return
    project = project.resolve()
    gen_root = Path(recomp["gen_root"]).expanduser()
    gen_root = gen_root if gen_root.is_absolute() else base.parent / gen_root
    state = gen_root / f"rekey-{version}.json"
    wanted = digest(project, recomp, version)
    if project == Path(data["project_root"]).resolve():
        derived = base
    else:
        derived = base.parent / "rekey" / f"{hashlib.sha256(str(project).encode()).hexdigest()[:12]}.json"
        _write(derived, json.dumps(derive(data, project), indent=1, sort_keys=True) + "\n")
    gen_root.mkdir(parents=True, exist_ok=True)

    def current() -> bool:
        try:
            return (
                json.loads(state.read_text()).get("digest") == wanted
                and (gen_root / version / "MANIFEST.json").is_file()
            )
        except (OSError, ValueError):
            return False

    with (gen_root / "rekey.lock").open("a+b") as lock:
        while True:
            fcntl.flock(lock, fcntl.LOCK_SH)  # comparisons in flight share the generated output
            if current():
                break
            fcntl.flock(lock, fcntl.LOCK_EX)  # a regeneration waits for them and holds everyone else off
            if not current():
                _regenerate(profile, derived, data, base, version)
                _write(state, json.dumps({"digest": wanted, "project": str(project), "at": time.time()}) + "\n")
            fcntl.flock(lock, fcntl.LOCK_SH)
            if current():
                break
        with tools.taste_profile_override(derived):
            yield
