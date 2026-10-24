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
