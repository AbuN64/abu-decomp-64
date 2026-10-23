"""Real call states for `behave`: further entries of the function in the covering movie, each as its own case.

The oracle capture stops at the first entry of the function and freezes RDRAM there; every generated case then runs
over that one memory, so only one call state of the game is real. The coverage table says how often the movie enters
the function (`oracle.count`), and `taste repro capture --hit N` stops at the Nth entry. This module picks a handful of
entries spread over the part of the movie the time limit covers, captures each, and turns it into a case: its entry
registers, what the stubbed callees did, and the memory that differs from the first entry's image as bounded patches
(per case the harness copies the first image and applies the patches, so RDRAM is never stored once per hit).
One rule for every function: nothing here names a function, a version or an address.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

WANTED = 12  # entries (the first included) a run asks for; fewer when the movie enters the function less often
SPAN_POLLS = 600  # polls past the first entry a later entry may take (the oracle time limit follows: 0.1 s per poll)
GROWTH = 1.4
MAX_PATCH_BYTES = 2 << 20  # memory a case may patch over the first image; a hit that differs more is left out
MERGE_GAP = 256  # differing runs this close are one patch (the bytes between are equal to the base: harmless)
CHUNK = 1 << 17  # bytes in one `mem` line's patch (the harness reads lines of at most 4 MiB, hex doubles them)
COARSE = 4096
FINE = 64


def choose(count: int, wanted: int = WANTED) -> list[int]:
    """Entry numbers (from 2) to capture besides the first, ascending: every entry while they are few, then growing
    by 40% so early, middle and late states of the movie are all there. The oracle ends a replay after the movie, so
    a late entry may not exist in a replay (`real_cases` stops at the first one that does not)."""
    picked: list[int] = []
    at = 2
    while len(picked) < wanted - 1 and at <= count:
        picked.append(at)
        at = max(at + 1, int(at * GROWTH))
    return picked


def seconds(minimum: int, first_poll: int, poll_seconds: float, span: int = SPAN_POLLS) -> int:
    """Oracle time limit for a later entry: the boot, the replay to the first entry and the span past it."""
    return minimum + int((first_poll + span) * poll_seconds + 0.999)


def differing(base: bytes, other: bytes) -> list[tuple[int, int]]:
    """(offset, end) runs where `other` differs from `base`, runs closer than MERGE_GAP joined."""
    size = min(len(base), len(other))
    runs: list[list[int]] = []
    for start in range(0, size, COARSE):
        stop = min(size, start + COARSE)
        if base[start:stop] == other[start:stop]:
            continue
        for fine in range(start, stop, FINE):
            end = min(stop, fine + FINE)
            if base[fine:end] == other[fine:end]:
                continue
            if runs and fine - runs[-1][1] <= MERGE_GAP:
                runs[-1][1] = end
            else:
                runs.append([fine, end])
    return [(a, b) for a, b in runs]


def patches(base: bytes, other: bytes, ram_base: int, limit: int = MAX_PATCH_BYTES) -> list[dict[str, str]] | None:
    """The memory patches that turn `base` into `other` ({"addr", "hex"}, equal bytes inside a merged run included),
    or None when they exceed `limit` bytes (such a hit is no small variation of the first image)."""
    runs = differing(base, other)
    if sum(b - a for a, b in runs) > limit:
        return None
    rows = []
    for a, b in runs:
        for at in range(a, b, CHUNK):
            end = min(b, at + CHUNK)
            rows.append({"addr": f"{ram_base + at:#x}", "hex": other[at:end].hex()})
    return rows


def case_of(seed: dict[str, Any], base: bytes, ram_base: int, limit: int = MAX_PATCH_BYTES) -> dict[str, Any] | None:
    """The case for one captured entry over the first entry's image `base`, labelled as a real state; None when its
    memory differs from the base by more than `limit`."""
    other = Path(seed["capture"]["rdram_path"]).read_bytes()
    rows = patches(base, other, ram_base, limit)
    if rows is None:
        return None
    case = dict(seed["case"])
    case["labels"] = {"seed": case["name"], "hit": str(seed["capture"]["hit"])}
    if rows:
        case["memory"] = rows
    return case


def signature(case: dict[str, Any]) -> str:
    """What makes a real state distinct: its registers, its memory patches and the stub answers."""
    return repr(
        (
            sorted((str(k), str(v)) for k, v in case.get("registers", {}).items()),
            [(m["addr"], m["hex"]) for m in case.get("memory", [])],
            str(case.get("stub_calls")),
        )
    )


def real_cases(
    entry_seed: dict[str, Any],
    hits: list[int],
    capture: Any,
    ram_base: int = 0x80000000,
    jobs: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The cases of the other entries `hits` (ascending; capture(hit) -> seed | the reason it failed) over the first
    entry's image, and one note per entry left out. `jobs` captures run at once; the first entry the replay never
    reaches ends the list (a later one cannot be there either)."""
    if not hits:
        return [], []
    base = Path(entry_seed["capture"]["rdram_path"]).read_bytes()
    seen = {signature(entry_seed["case"])}
    cases: list[dict[str, Any]] = []
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for start in range(0, len(hits), max(1, jobs)):
            group = hits[start : start + max(1, jobs)]
            lost = False
            for hit, seed in zip(group, pool.map(capture, group), strict=True):
                if not isinstance(seed, dict):
                    notes.append(f"entry {hit}: {seed or 'not captured'}")
                    lost = True
                    continue
                case = case_of(seed, base, ram_base, MAX_PATCH_BYTES)
                if case is None:
                    far = MAX_PATCH_BYTES >> 20
                    notes.append(f"entry {hit}: memory differs from the first entry's by over {far} MiB")
                elif (sig := signature(case)) in seen:
                    notes.append(f"entry {hit}: the same call state as an earlier entry")
                else:
                    seen.add(sig)
                    cases.append(case)
            if lost:
                break
    return cases, notes
