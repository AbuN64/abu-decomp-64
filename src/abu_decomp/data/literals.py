"""TU-local float pool constants as literals (F3): the shortest decimal text that round-trips the init word."""

from __future__ import annotations

import re
import struct


def float_literal(init: bytes, kind: str) -> str | None:
    """`1.0f` / `0.99f` for an f32 word, a repr for an f64; None when the bytes are short or have no short form."""
    if kind == "f32" and len(init) >= 4:
        v = struct.unpack(">f", init[:4])[0]
        t = None
        for n in range(1, 10):
            c = ("%.*f" % (n, v)).rstrip("0") if abs(v) >= 1e-4 else "%.*g" % (n, v)
            if "e" not in c and struct.pack(">f", float(c)) == init[:4]:
                t = c
                break
        if t is None:
            for n in range(1, 18):
                c = "%.*f" % (n, v)
                if struct.pack(">f", float(c)) == init[:4]:
                    t = c.rstrip("0")
                    break
        if t is None:
            return None
        if t.endswith("."):
            t += "0"
        return (t if "." in t else t + ".0") + "f"
    if kind == "f64" and len(init) >= 8:
        r = repr(struct.unpack(">d", init[:8])[0])
        return r if "e" not in r else None
    return None


def literal_for(info: dict | None) -> str | None:
    """Literal text for a census member dict (literal flag, init hex, access types)."""
    if not info or not info.get("literal") or not info.get("init"):
        return None
    acc = list(info.get("access", {}))
    if not acc:
        return None
    return float_literal(bytes.fromhex(info["init"]), acc[0].split(":", 1)[-1])


_HEX8 = re.compile(r"[0-9A-Fa-f]{8}")


def _word_name(sym: str, off: int) -> str:
    """The symbol name of the word OFF bytes into SYM's record: its address digits advanced, the rest of the name kept."""
    m = _HEX8.search(sym)
    if not m:
        return f"{sym}+0x{off:X}"
    digits = m.group(0)
    return (
        sym[: m.start()] + format(int(digits, 16) + off, "08X" if digits == digits.upper() else "08x") + sym[m.end() :]
    )


def interior_words(info: dict, listed: set[int]) -> list[str]:
    """The names of the 4-byte words inside a literal record that its own reads leave out and no census record lists: the
    record holds more initializer than its accesses cover, so replacing it by literals leaves those words unplaced."""
    widths = [int(k.split(":")[0]) for k in info.get("access", {}) if k.split(":")[0].isdigit()]
    if not info.get("literal") or not widths or not info.get("addr"):
        return []
    init = len(info.get("init") or "") // 2
    end = (
        max(init, info.get("size") or 0) if init >= 16 else init
    )  # init is cut at 16 bytes; below that it is the record
    base = int(info["addr"], 16)
    return [_word_name(info["sym"], off) for off in range(-(-max(widths) // 4) * 4, end, 4) if base + off not in listed]


def unlisted_interior(census: dict) -> dict[str, list[str]]:
    """literal record sym -> its interior words no census record lists (only the records that have some)."""
    members = [m for c in census["clusters"] for m in c["members"]] + census["lones"]
    listed = {int(m["addr"], 16) for m in members if m.get("addr")}
    return {m["sym"]: w for m in members if (w := interior_words(m, listed))}


def interior_refusal(sym: str, words: list[str]) -> str:
    return (
        f"{sym}: its record holds {', '.join(words)}, which no census record lists and its reads do not cover, so the pool "
        f"and its readers are unproved; list the word in the census before replacing {sym} by a literal"
    )


def apply(body: str, lits: dict[str, str], raw: re.Pattern[str]) -> str:
    return raw.sub(lambda m: lits.get(m.group(1), m.group(0)), body)


def drop_dead_externs(src: str, syms: list[str]) -> str:
    """Delete the extern declarators of SYMS that nothing but their own declaration still names."""
    for s in syms:
        uses = list(re.finditer(r"\b%s\b" % s, src))
        if uses and all(
            re.search(r"\bextern\b[^;]*$", src[max(0, m.start() - 200) : m.start()].split(";")[-1]) for m in uses
        ):
            src = re.sub(r",\s*%s\b(\s*\[[^\]]*\])?" % s, "", src)
            src = re.sub(r"(\bextern\s+[^;,]*?)\b%s\b(\s*\[[^\]]*\])?\s*,\s*" % s, r"\1", src)
            src = re.sub(r"^[ \t]*extern\s+[^;,]*\b%s\b(\s*\[[^\]]*\])?\s*;[ \t]*\n" % s, "", src, flags=re.M)
    return src
