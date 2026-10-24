"""Text edits on C sources: NON_MATCHING wrappers, function bodies, extern removal and the owner-expression rewrite."""

from __future__ import annotations

import re

from abu_decomp.data import cdecl

NM_WRAP = r"#ifdef NON_MATCHING\n((?:(?!#ifdef NON_MATCHING)[\s\S])*?)#else\nINCLUDE_ASM\(\"[^\"]+\", %s\);\n#endif\n"
SPANS = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', re.S)


def unwrap(src: str, fn: str) -> str:
    """The draft of FN inside its `#ifdef NON_MATCHING ... #else INCLUDE_ASM #endif` wrapper, wrapper removed."""
    m = re.search(NM_WRAP % re.escape(fn), src)
    return (src[: m.start()] + m.group(1) + src[m.end() :]) if m else src


def body_span(src: str, fn: str) -> tuple[int, int] | None:
    m = re.search(r"^[A-Za-z_][\w \*]*\b%s\s*\([^;{]*\)\s*\{" % re.escape(fn), src, re.M)
    if not m:
        return None
    end = src.find("\n}\n", m.end())
    return (m.end(), end + 2) if end > 0 else None


def spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of comments and string literals: never rewritten."""
    return [m.span() for m in SPANS.finditer(text)]


def drop_externs(text: str, sym: str) -> str:
    """Delete every extern declarator of SYM (a whole line when it was the only declarator)."""
    tok = re.compile(r"(?<![\w])%s\b" % re.escape(sym))

    def drop(m: re.Match[str]) -> str:
        body = m.group(0)
        if not tok.search(body):
            return body
        parts = re.split(r",(?![^\[]*\])", body[len("extern") : -1])
        if len(parts) == 1:
            return "\x00DEL\x00"
        keep = [p for p in parts if not tok.search(p)]
        if tok.search(parts[0]):  # SYM was the first declarator: the next one takes the type
            typ = parts[0][: tok.search(parts[0]).start()].rstrip("* \t")
            keep[0] = " " + typ.strip() + " " + keep[0].strip()
        return "extern" + ",".join(keep) + ";"

    text = re.sub(r"\bextern\b[^;{}()]*;", drop, text)
    return re.sub(r"[ \t]*\x00DEL\x00[ \t]*(?://[^\n]*|/\*[^\n]*?\*/)?[ \t]*\n?", "", text)


def replace_uses(text: str, sym: str, expr: str) -> str:
    tok = re.compile(r"(?<![\w])%s\b" % re.escape(sym))
    sp = spans(text)
    out, last = [], 0
    for m in tok.finditer(text):
        if any(s <= m.start() < e for s, e in sp):
            continue
        out += [text[last : m.start()], expr]
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def local_decls(text: str, sym: str) -> tuple[list[str], list[str]]:
    """(extern type texts, unit-local definition type texts) of SYM in TEXT."""
    plain = cdecl.strip_comments(text)
    token = re.compile(r"(?<![\w])%s\b" % re.escape(sym))
    ext = [t for m in cdecl.CDECL.finditer(plain) for s, t in cdecl.declarators(m.group(1), token) if s == sym]
    ldef = re.compile(cdecl.LOCALDEF % re.escape(sym), re.M)
    local = [
        m.group(1).strip() + m.group(3)
        for m in ldef.finditer(plain)
        if m.group(1).strip().split()[0] not in ("return", "extern", "typedef")
    ]
    return ext, local


def skip_reason(text: str, sym: str, owner: str, kinds_all: set[str], access: dict | None) -> str:
    """Why this file must keep the raw SYM ('' when the rewrite is safe): a unit-local definition or a struct alias of
    a symbol other units declare as a scalar (rewriting would re-type the access), a declared kind that contradicts how
    the ROM accesses it, or an owner declared here as something that has no members."""
    ext, local = local_decls(text, sym)
    if local:
        return f"unit-local definition of {sym}: {local[0]}"
    kinds = cdecl.decl_kind(ext)
    if "s" in kinds and kinds_all - {"s"}:
        return f"struct alias of {sym} here ({ext[0]}) where other units declare it as a scalar"
    seen = {"f" if k.split(":")[1][:1] == "f" else "i" for k in (access or {}) if ":" in k}
    mine = {k for k in kinds if k in ("i", "f")}
    if seen and mine and not (seen & mine):
        return f"declared {ext[0]} but the ROM accesses it as {'/'.join(sorted(seen))}"
    o_ext, o_local = local_decls(text, owner)
    if o_local or (o_ext and cdecl.decl_kind(o_ext) - {"s"}):
        return f"owner {owner} is declared here as {(o_local or o_ext)[0]}, not as its struct"
    return ""


def add_include(text: str, owner_expr: str, header: str, is_header_file: bool) -> str:
    """Include HEADER after the first #include when the text now names the owner outside the leading include block; a
    file that declares the owner itself (extern or definition) already has it and gets no second, conflicting copy."""
    owner = re.match(r"\w+", owner_expr).group(0)
    if any(local_decls(text, owner)):
        return text
    code = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    lead = re.match(r"(?:\s*(?:#include [^\n]*|#ifndef [^\n]*|#define [^\n]*|/\*.*?\*/)\n)*", text, re.S).group(0)
    if is_header_file or not re.search(r"\b%s\b" % owner, code) or ('"%s"' % header) in lead:
        return text
    inc = re.search(r"^#include .*\n", text, re.M)
    at = inc.end() if inc else 0
    return text[:at] + '#include "%s"\n' % header + text[at:]


def file_expr(text: str, sym: str, expr: str, decls: list[str] | None = None) -> str:
    """EXPR as this file uses it. The census names the leaf at the symbol's address (`O.regs[0]`); a file that declares the
    symbol as an array (or, with no declaration of its own, whose header does: DECLS) uses the whole member (`O.regs`),
    or `regs[0][i]` would index the element, not the array."""
    ext, _local = local_decls(text, sym)
    array = any("[" in t for t in (ext or decls or []))
    return expr[:-3] if expr.endswith("[0]") and array else expr


def plan_file(
    text: str,
    sym: str,
    expr: str,
    header: str,
    is_header_file: bool = False,
    member: dict | None = None,
) -> tuple[str, str]:
    """(new text, skip reason): the per-file rewrite of rewrite.py. A file with a reason keeps its text. The safety
    rules run when the census MEMBER of the symbol is given (kinds every unit declares it with, ROM access, decl types)."""
    if sym not in text:
        return text, ""
    owner = re.match(r"\w+", expr).group(0)
    kinds = set(member.get("decl_kind") or []) if member is not None else None
    why = skip_reason(text, sym, owner, kinds, member.get("access")) if member is not None else ""
    if why:
        return text, why
    use = file_expr(text, sym, expr, (member or {}).get("decl"))
    new = replace_uses(drop_externs(text, sym), sym, use)
    return (add_include(new, expr, header, is_header_file) if new != text else text), ""


def rewrite_file(text: str, sym: str, expr: str, header: str, is_header_file: bool = False) -> str:
    """The whole per-file rewrite of rewrite.py: externs out, uses to EXPR, owner include in."""
    return plan_file(text, sym, expr, header, is_header_file)[0]


def put_back(text: str, fn: str, expr: str, sym: str, decl: str) -> str:
    """MIXED exception: inside FN's body the owner form EXPR goes back to the scalar SYM, and a local
    `extern DECL;` is added at the top of the body when the unit does not declare SYM any more."""
    sp = body_span(text, fn)
    if not sp:
        return text
    body = text[sp[0] : sp[1]]
    sp_ = spans(body)
    out, last = [], 0
    for m in re.finditer(re.escape(expr) + r"(?![\w\[.])", body):
        if any(s <= m.start() < e for s, e in sp_):
            continue
        out += [body[last : m.start()], sym]
        last = m.end()
    out.append(body[last:])
    nb = "".join(out)
    if nb == body:
        return text
    text = text[: sp[0]] + nb + text[sp[1] :]
    if not re.search(r"\bextern\b[^;]*\b%s\b" % re.escape(sym), text):
        inc = list(re.finditer(r"^#include .*\n", text, re.M))
        at = inc[-1].end() if inc else 0
        return text[:at] + f"extern {decl} {sym}; /* MIXED {fn} */\n" + text[at:]
    return text
