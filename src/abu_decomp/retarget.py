"""Carry an in-flight lane patch across the renames main made since the patch was cut.

`recipe apply` renames symbols (every identifier in src/ and include/, and the symbol_addrs lines) and moves files
(source and header renames, with the includes). A patch cut before that names the old things, so `git apply` finds no
file and no context. Landing translates the patch instead of refusing it:

  base      the newest commit whose files match the patch's pre-image blobs (its `index` lines).
  renames   from that base to HEAD: files git sees as renamed (`diff -M`), and symbols a `symbol_addrs.txt` line
            gives a new name at the same address (an address names one thing, so the pair is certain). A name the tree
            still defines is never a rename.
  rewrite   the pre-image and the post-image of every file of the patch (the patch applied in a scratch index)
            get the identifier renames (word-bounded) and the include renames, go to their renamed paths, and the
            patch is cut again between the two as a git diff with real blobs, so `git apply --3way` still merges.

A patch that names no renamed path or identifier comes back untouched. The result says what was rewritten."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Git = Callable[..., str]
_INDEX = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", re.M)
_SYMBOL = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*=\s*(0[xX][0-9A-Fa-f]+|\d+)\s*;", re.M)
_FACTS = "versions/*/symbol_addrs.txt"


@dataclass
class Renames:
    base: str = ""
    files: dict[str, str] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.files or self.symbols)


def sections(text: str) -> list[tuple[str, str, str]]:
    """(old path, new path, section) per `diff --git` header of a git patch."""
    out: list[tuple[str, list[str]]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            out.append((line, []))
        if out:
            out[-1][1].append(line)
    rows = []
    for header, lines in out:
        old, _, new = header.rstrip("\n")[len("diff --git a/") :].partition(" b/")
        rows.append((old, new, "".join(lines)))
    return rows


def find_base(git: Git, patch: str) -> str:
    """The newest commit on HEAD's history at which the files of the patch have the blobs it was cut against."""
    best = ""
    for old, _new, section in sections(patch):
        found = _INDEX.search(section)
        if not found or set(found[1]) == {"0"}:
            continue
        for commit in git("log", "--format=%H", "--", old, ok=False).split():
            if git("rev-parse", f"{commit}:{old}", ok=False).strip().startswith(found[1]):
                if not best or git("rev-list", "--count", f"{best}..{commit}", ok=False).strip() not in ("", "0"):
                    best = commit
                break
    return best


def symbol_pairs(before: str, after: str, still: set[str]) -> dict[str, str]:
    """Names a fact file gave a new name at the same address, between two texts of it."""

    def table(text: str) -> dict[int, set[str]]:
        rows: dict[int, set[str]] = {}
        for name, address in _SYMBOL.findall(text):
            rows.setdefault(int(address, 0), set()).add(name)
        return rows

    old, new = table(before), table(after)
    pairs: dict[str, str] = {}
    for address, names in old.items():
        gone, came = names - new.get(address, set()), new.get(address, set()) - names
        if len(gone) == 1 and len(came) == 1:
            (was,), (now,) = gone, came
            if was not in still:
                pairs[was] = now
    return pairs


def renames_since(git: Git, base: str) -> Renames:
    found = Renames(base=base)
    for row in git("diff", "-M", "--name-status", base, "HEAD", ok=False).splitlines():
        parts = row.split("\t")
        if parts[0].startswith("R") and len(parts) == 3:
            found.files[parts[1]] = parts[2]
    facts = git("ls-files", "--", _FACTS, ok=False).split()
    still = {n for path in facts for n, _ in _SYMBOL.findall(git("show", f"HEAD:{path}", ok=False))}
    for path in git("diff", "--name-only", base, "HEAD", "--", _FACTS, ok=False).split():
        found.symbols.update(
            symbol_pairs(git("show", f"{base}:{path}", ok=False), git("show", f"HEAD:{path}", ok=False), still)
        )
    return found


def rewrite(text: str, renames: Renames) -> str:
    if renames.symbols:
        pattern = re.compile(
            r"(?<![\w])(" + "|".join(map(re.escape, sorted(renames.symbols, key=len, reverse=True))) + r")(?![\w])"
        )
        text = pattern.sub(lambda m: renames.symbols[m[1]], text)
    for old, new in renames.files.items():
        if old.lower().endswith(
            (".h", ".hh", ".hpp", ".hxx")
        ):  # the includes that name a moved header, spelled with or without include/
            for spelled, target in ((old, new), (old.removeprefix("include/"), new.removeprefix("include/"))):
                text = re.sub(
                    rf'(#[ \t]*include[ \t]*[<"]){re.escape(spelled)}([>"])',
                    lambda m, t=target: f"{m[1]}{t}{m[2]}",
                    text,
                )
    return text


def mentions(patch: str, renames: Renames) -> bool:
    if any(old in patch for old in renames.files):
        return True
    return any(re.search(rf"(?<![\w]){re.escape(old)}(?![\w])", patch) for old in renames.symbols)


def translate(git: Git, patch_text: str) -> tuple[str, Renames]:
    """The patch re-cut through the renames since its base; (patch, renames), the original text when nothing applies."""
    base = find_base(git, patch_text)
    if not base:
        return patch_text, Renames()
    renames = renames_since(git, base)
    if not renames or not mentions(patch_text, renames):
        return patch_text, renames
    git_dir = Path(git("rev-parse", "--absolute-git-dir").strip())
    rows = [(old, new) for old, new, _s in sections(patch_text)]
    with tempfile.TemporaryDirectory(prefix="LAND_RETARGET_", dir=git_dir) as work:
        patch_file = os.path.join(work, "patch")
        Path(patch_file).write_text(patch_text)
        side = {n: {"GIT_INDEX_FILE": os.path.join(work, n)} for n in ("pre", "post", "cut-pre", "cut-post")}
        git("read-tree", base, env=side["pre"])
        git("read-tree", base, env=side["post"])
        git("apply", "--cached", patch_file, env=side["post"])
        for which, pick in (("pre", 0), ("post", 1)):
            for row in rows:
                name = row[pick]
                listing = git("ls-files", "-s", "--", name, env=side[which], ok=False).split()
                if not listing:
                    continue
                content = git("cat-file", "-p", listing[1], env=side[which])
                if "\ufffd" in content:
                    return patch_text, renames  # not text we can rewrite safely
                blob_file = os.path.join(work, "blob")
                Path(blob_file).write_text(rewrite(content, renames))
                blob = git("hash-object", "-w", blob_file).strip()
                git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{listing[0]},{blob},{renames.files.get(name, name)}",
                    env=side["cut-" + which],
                )
        before = git("write-tree", env=side["cut-pre"]).strip()
        after = git("write-tree", env=side["cut-post"]).strip()
        text = git("diff", "--full-index", "--binary", "--no-color", before, after)
    return text, renames
