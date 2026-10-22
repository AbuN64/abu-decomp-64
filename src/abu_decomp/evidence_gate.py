"""The measuring half of the evidence gate: behave runs on the parent's and the candidate's file, cached on disk.

The cache lives in the behave work directory and is keyed by (function, source text of the file, hash of the header
paths' git tree, ROM digests, installed tool versions), so the parent side is normally a hit: the same parent text was
measured by the previous gate. A measure that fails to run is a refusal (the gate never passes on a blank).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from abu_decomp import DecompError, evidence, meter, tools

if TYPE_CHECKING:
    from abu_decomp.land import Landing


class Gate:
    def __init__(self, landing: Landing, config: dict[str, Any], versions: Any = tools.version_text) -> None:
        self.landing, self.config, self.versions = landing, config, versions
        self.measures: list[dict[str, Any]] = []
        self.sources: dict[str, tuple[str, Path]] = {}  # side -> (text, file the behave candidate reads)
        self.trees: dict[str, str] = {}
        self.roms: str | None = None

    # keys ----------------------------------------------------------------------------------------------------------
    def header_hash(self, side: str) -> str:
        tree = self.trees.get(side)
        if tree is None:
            tree = self.landing.extra["base"] if side == "base" else self.landing.snapshot()
            self.trees[side] = tree
        listing = self.landing.git("ls-tree", "-r", tree, "--", *self.config["headers"], ok=False)
        return hashlib.sha256(listing.encode()).hexdigest()

    def rom_hash(self) -> str:
        if self.roms is None:
            digest = hashlib.sha1()
            for pattern in self.config["roms"]:
                for path in sorted(self.landing.tree.glob(pattern)):
                    digest.update(path.name.encode())
                    digest.update(hashlib.sha1(path.read_bytes()).digest())
            self.roms = digest.hexdigest()
        return self.roms

    def key(self, side: str, function: str, text: str) -> str:
        parts = [
            function,
            hashlib.sha256(text.encode()).hexdigest(),
            self.header_hash(side),
            self.rom_hash(),
            self.versions(),
        ]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def cache_dir(self) -> Path:
        from abu_decomp.behave.run import configuration, work_dir

        path = work_dir(configuration(self.landing.profile)) / "evidence"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # measuring -----------------------------------------------------------------------------------------------------
    def candidate(self, side: str, relative: str, text: str) -> Path:
        if side == "new":
            return self.landing.tree / relative
        path = self.cache_dir() / f"parent-{hashlib.sha256(text.encode()).hexdigest()[:16]}" / Path(relative).name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(errors="replace") != text:
            path.write_text(text)
        return path

    def measure(self, side: str, relative: str, text: str, function: str) -> evidence.Score:
        key = self.key(side, function, text)
        cached = self.cache_dir() / f"{key}.json"
        if cached.is_file():
            data = json.loads(cached.read_text())
            self.measures.append({"function": function, "side": side, "cached": True, "seconds": 0.0, "cpu": 0.0})
            return evidence.Score(**data)
        path = self.candidate(side, relative, text)
        started, cpu = time.monotonic(), meter.cpu()
        try:
            result = self.run_behave(function, path)
            counts = result.get("counts") or {}
            score = evidence.Score(
                int(counts.get("same", 0)),
                int(result.get("total", 0)),
                (result.get("candidate") or {}).get("function_sha256"),
                str(result.get("reason") or "")[:200],
            )
        except DecompError as exc:
            raise EvidenceError(
                "land.evidence_unmeasured", f"{function} ({side}): behave failed: {exc.detail}"
            ) from exc
        wall, spent = time.monotonic() - started, meter.cpu() - cpu
        self.measures.append(
            {"function": function, "side": side, "cached": False, "seconds": round(wall, 1), "cpu": round(spent, 1)}
        )
        if wall > self.config["slow_seconds"]:
            self.landing.findings.append(
                {
                    "key": "land.evidence_slow",
                    "blocking": False,
                    "message": f"behave of {function} ({side}) took {wall:.0f}s wall, {spent:.0f}s CPU",
                }
            )
        cached.write_text(json.dumps(score.__dict__))
        return score

    def run_behave(self, function: str, path: Path) -> dict[str, Any]:
        from abu_decomp.behave.run import Options, run

        options = Options(function, path, None, None, 1, self.landing.tree, None)
        call = self.landing.behave or (lambda o: run(self.landing.profile, o))
        result, _, _ = call(options)
        return result

    def run(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        landing, cfg = self.landing, self.config
        refused: list[dict[str, str]] = []
        checked: list[dict[str, str]] = []
        files = [f for f in landing.changed_files() if f.lower().endswith(cfg["suffixes"])]
        for relative in files:
            old = landing.git("show", f"{landing.extra['base']}:{relative}", ok=False)
            path = landing.tree / relative
            new = path.read_text(errors="replace") if path.is_file() else ""
            base_fns = evidence.parse(old, cfg["guard"], cfg["stand_in"])
            new_fns = evidence.parse(new, cfg["guard"], cfg["stand_in"])
            texts = {"base": old, "new": new}

            def measure(
                side: str, function: str, relative: str = relative, texts: dict[str, str] = texts
            ) -> evidence.Score:
                return self.measure(side, relative, texts[side], function)

            try:
                bad, good = evidence.judge(base_fns, new_fns, measure)
            except EvidenceError as exc:
                bad, good = [{"function": relative, "base": "?", "new": "?", "why": exc.detail}], []
            profile = landing.profile.source_file
            for row in bad:
                new_path = self.candidate("new", relative, new)
                row["file"] = relative
                row["reproduce"] = (
                    f"decomp --profile {profile} behave --function {row['function']} --candidate {new_path} "
                    f"--project {landing.tree}"
                )
            refused += bad
            checked += [{**row, "file": relative} for row in good]
        return refused, checked


class EvidenceError(DecompError):
    pass
