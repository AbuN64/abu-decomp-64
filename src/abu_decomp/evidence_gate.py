"""The measuring half of the evidence gate: behave runs on the parent's and the candidate's file, cached on disk.

The cache lives in the behave work directory and is keyed by (function, source text of the file, hash of the header
paths' git tree, ROM digests, installed tool versions), so the parent side is normally a hit: the same parent text was
measured by the previous gate. A measure that fails to run is a refusal (the gate never passes on a blank).
"""

from __future__ import annotations

import hashlib
import json
import re
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


_TOKEN = re.compile(r"[A-Za-z_]\w*|\d[\w.]*|\S")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_C_SOURCES = (".c", ".cpp", ".cc", ".h", ".hpp")


def rename_pairs(before: evidence.Fn, after: evidence.Fn) -> list[tuple[str, str]] | None:
    """The (old, new) identifier pairs that turn one function text into the other token for token, [] for equal
    tokens, None when anything but a non-keyword identifier differs or the pairs are not one-to-one."""
    if before.kind != after.kind:
        return None
    one, two = _TOKEN.findall(before.text), _TOKEN.findall(after.text)
    if len(one) != len(two):
        return None
    forward: dict[str, str] = {}
    backward: dict[str, str] = {}
    for old, new in zip(one, two, strict=True):
        if old == new:
            continue
        if not (_IDENT.fullmatch(old) and _IDENT.fullmatch(new)):
            return None
        if old in provenance._FIXED or new in provenance._FIXED or old in provenance._TYPE_WORDS:
            return None
        if forward.setdefault(old, new) != new or backward.setdefault(new, old) != old:
            return None
    return sorted(forward.items())


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

    def provenance(
        self,
        relative: str,
        base_fns: dict[str, evidence.Fn],
        new_fns: dict[str, evidence.Fn],
        old: str,
        new: str,
    ) -> list[dict[str, str]]:
        """Refusals for the machine-generated leftovers a changed body adds."""
        rows = provenance.leftover_names(base_fns, new_fns, old, new, self.config.get("names") or [])
        profile = self.landing.profile.source_file
        return [
            {
                "function": row["function"],
                "base": "body",
                "new": "leftover names",
                "why": row["why"],
                "file": relative,
                "reproduce": f"decomp --profile {profile} recipe style {relative}",
            }
            for row in rows
        ]

    def renamed(self, pair: tuple[str, str]) -> bool:
        """Whether (old, new) is a rename of the whole landing: no C source of the tree holds the old name any more
        (every caller and declaration moved with it) and the base tree never held the new one (no capture)."""
        old, new = pair
        spec = [f":(icase)*{suffix}" for suffix in sorted({*self.config["suffixes"], *_C_SOURCES})]
        if self.landing.git("grep", "--untracked", "-lw", "-e", old, "--", *spec, ok=False).strip():
            return False
        base = self.landing.extra["base"]
        return not self.landing.git("grep", "-lw", "-e", new, base, "--", *spec, ok=False).strip()

    def identifier_only(self, base_fns: dict[str, evidence.Fn], new_fns: dict[str, evidence.Fn]) -> set[str]:
        """The functions whose only change is renamed identifiers: the same tokens modulo a one-to-one map that
        renames those names across the whole tree (what `unbake renameproof` proves for a change). Such a function
        keeps whatever rung its parent held and owes no try and no behave evidence."""
        lost = {n: f for n, f in base_fns.items() if n not in new_fns}
        found: dict[str, str] = {}
        for name, fn in sorted(new_fns.items()):
            parent = base_fns.get(name)
            if parent is not None and parent == fn:
                continue
            options = [(name, parent)] if parent is not None else sorted(lost.items())
            for old_name, before in options:
                if old_name in found.values():
                    continue
                pairs = rename_pairs(before, fn)
                if pairs and all(self.renamed(pair) for pair in pairs):
                    found[name] = old_name
                    break
        return set(found)

    def run(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        landing, cfg = self.landing, self.config
        refused: list[dict[str, str]] = []
        checked: list[dict[str, str]] = []
        files = [f for f in landing.changed_files() if f.lower().endswith(cfg["suffixes"])]
        lost: dict[
            str, str
        ] = {}  # function -> the file whose base held its body or INCLUDE_ASM line and the tree lacks it
        gained: dict[str, str] = {}  # function -> a file that holds it now and did not at the base
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

            # an identifier-only change owes no behave evidence: it keeps its parent's rung
            # a body full of leftovers is refused without paying for its behave measure
            same = self.identifier_only(base_fns, new_fns)
            kept = {n: f for n, f in new_fns.items() if n not in same}
            blocked = self.provenance(relative, base_fns, kept, old, new)
            judged = {n: f for n, f in kept.items() if n not in {row["function"] for row in blocked}}
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
