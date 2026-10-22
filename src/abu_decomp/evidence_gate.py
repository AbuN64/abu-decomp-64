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

from abu_decomp import DecompError, evidence, meter, provenance, tools

if TYPE_CHECKING:
    from abu_decomp.land import Landing


def header_digest(listing: str) -> str:
    return hashlib.sha256(listing.encode()).hexdigest()


def rom_digest(tree: Path, patterns: list[str]) -> str:
    digest = hashlib.sha1()
    for pattern in patterns:
        for path in sorted(tree.glob(pattern)):
            digest.update(path.name.encode())
            digest.update(hashlib.sha1(path.read_bytes()).digest())
    return digest.hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
        return header_digest(listing)

    def rom_hash(self) -> str:
        if self.roms is None:
            self.roms = rom_digest(self.landing.tree, self.config["roms"])
        return self.roms

    def key(self, side: str, function: str, text: str) -> str:
        parts = [
            function,
            text_digest(text),
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
            return evidence.Score(**{k: v for k, v in data.items() if k in evidence.Score.__dataclass_fields__})
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
        # the sidecar fields let `decomp owes` find this measure again after the tool version moved the key
        facts = {
            "function": function,
            "text": text_digest(text),
            "headers": self.header_hash(side),
            "roms": self.rom_hash(),
        }
        cached.write_text(json.dumps({**score.__dict__, **facts}))
        return score

    def run_behave(self, function: str, path: Path) -> dict[str, Any]:
        from abu_decomp.behave.run import Options, run

        options = Options(function, path, None, None, 1, self.landing.tree, None)
        call = self.landing.behave or (lambda o: run(self.landing.profile, o))
        result, _, _ = call(options)
        return result

    def tries(self) -> dict[str, dict[str, Any]] | None:
        """The bodies `recipe try` recorded (None: the provenance check is off)."""
        given = self.config.get("provenance")
        if given is None:
            return None
        root = Path(str(tools.project_dir(self.landing.profile)))
        return provenance.recorded([root / t for t in given["tries"]])

    def provenance(
        self,
        relative: str,
        base_fns: dict[str, evidence.Fn],
        new_fns: dict[str, evidence.Fn],
        old: str,
        new: str,
        tries: dict[str, dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        """Refusals for a body no `recipe try` produced and for machine-generated leftovers it adds."""
        rows = [] if tries is None else provenance.unproven(base_fns, new_fns, tries)
        rows += provenance.leftover_names(base_fns, new_fns, old, new, self.config.get("names") or [])
        profile = self.landing.profile.source_file
        out = []
        for row in rows:
            leftover = "leftovers" in row["why"]
            command = f"recipe style {relative}" if leftover else row["next"]
            out.append(
                {
                    "function": row["function"],
                    "base": "body",
                    "new": "leftover names" if leftover else "unproven body",
                    "why": row["why"],
                    "file": relative,
                    "reproduce": f"decomp --profile {profile} {command}",
                }
            )
        return out

    def run(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        landing, cfg = self.landing, self.config
        refused: list[dict[str, str]] = []
        checked: list[dict[str, str]] = []
        files = [f for f in landing.changed_files() if f.lower().endswith(cfg["suffixes"])]
        lost: dict[
            str, str
        ] = {}  # function -> the file whose base held its body or INCLUDE_ASM line and the tree lacks it
        gained: dict[str, str] = {}  # function -> a file that holds it now and did not at the base
        tries = self.tries()
        for relative in files:
            old = landing.git("show", f"{landing.extra['base']}:{relative}", ok=False)
            path = landing.tree / relative
            new = path.read_text(errors="replace") if path.is_file() else ""
            base_fns = evidence.parse(old, cfg["guard"], cfg["stand_in"])
            new_fns = evidence.parse(new, cfg["guard"], cfg["stand_in"])
            texts = {"base": old, "new": new}
            lost |= {name: relative for name in base_fns if name not in new_fns}
            gained |= {name: relative for name in new_fns if name not in base_fns}

            def measure(
                side: str, function: str, relative: str = relative, texts: dict[str, str] = texts
            ) -> evidence.Score:
                return self.measure(side, relative, texts[side], function)

            # a body no try produced, or full of leftovers, is refused without paying for its behave measure
            blocked = self.provenance(relative, base_fns, new_fns, old, new, tries)
            judged = {n: f for n, f in new_fns.items() if n not in {row["function"] for row in blocked}}
            try:
                bad, good = evidence.judge(base_fns, judged, measure)
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
            refused += bad + blocked
            checked += [{**row, "file": relative} for row in good]
        for name in sorted(lost.keys() & gained.keys()):
            home, there = lost[name], gained[name]
            refused.append(
                {
                    "function": name,
                    "base": f"in {home}",
                    "new": f"in {there}",
                    "why": f"{name} moved from unit {home} to unit {there}; its body belongs in place of its "
                    f"{cfg['stand_in']} line in {home}",
                    "file": there,
                    "reproduce": f"git -C {landing.tree} diff {landing.extra['base']} -- {home} {there}",
                }
            )
        return refused, checked


class EvidenceError(DecompError):
    pass
