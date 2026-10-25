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


def mask(text: str) -> str:
    """TEXT with every comment and string literal blanked to spaces (newlines kept): same length, same offsets, so a
    scan of the mask can never begin or end inside a comment (10-25 80444030_6: an `extern` inside a comment swallowed
    three functions up to the next `;`)."""
    return SPANS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


EXTERN = re.compile(r"\bextern\b[^;{}()]*;")


def drop_externs(text: str, sym: str) -> str:
    """Delete every extern declarator of SYM (a whole line when it was the only declarator). The scan runs on the masked
    text, so an `extern` inside a comment or string is never a declaration; kept text is cut from the original."""
    tok = re.compile(r"(?<![\w])%s\b" % re.escape(sym))
    masked = mask(text)
    out, last = [], 0
    for m in EXTERN.finditer(masked):
        body = m.group(0)
        if not tok.search(body):
            continue
        base = m.start() + len("extern")
        inner = body[len("extern") : -1]
        cuts = [0] + [c.end() for c in re.finditer(r",(?![^\[]*\])", inner)] + [len(inner) + 1]
        parts = [(base + cuts[i], base + cuts[i + 1] - 1) for i in range(len(cuts) - 1)]
        parts[-1] = (parts[-1][0], base + len(inner))
        out.append(text[last : m.start()])
        last = m.end()
        if len(parts) == 1:
            out.append("\x00DEL\x00")
            continue
        keep = [(s_, e_) for s_, e_ in parts if not tok.search(masked[s_:e_])]
        pieces = [text[s_:e_] for s_, e_ in keep]
        if tok.search(masked[parts[0][0] : parts[0][1]]):  # SYM was the first declarator: the next one takes the type
            first = masked[parts[0][0] : parts[0][1]]
            typ = first[: tok.search(first).start()].rstrip("* \t")
            pieces[0] = " " + typ.strip() + " " + pieces[0].strip()
        out.append("extern" + ",".join(pieces) + ";")
    out.append(text[last:])
    return re.sub(r"[ \t]*\x00DEL\x00[ \t]*(?://[^\n]*|/\*[^\n]*?\*/)?[ \t]*\n?", "", "".join(out))


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


def shape_reason(ext: list[str], expr: str) -> str:
    """Why the declared shape of the symbol in this file cannot be mapped onto the owner expression ('' when it can): a 2-D (or
    deeper) array declaration, or a 1-D array declared where the owner member is itself an array of arrays (`O.m[0][0]`), would
    be rewritten to `m[i]` forms that index the wrong dimension (10-24 DATA-B-S3: FontStyle, cell arrays). Written by hand."""
    dims = max((t.count("[") for t in ext), default=0)
    leaf = len(re.findall(r"\[0\]", re.search(r"(?:\[0\])*$", expr).group(0)))
    if dims >= 2:
        return f"{dims}-D array declaration here ({ext[0]}): the cell indexing cannot be mapped onto {expr}; rewrite by hand"
    if dims == 1 and leaf >= 2:
        return f"declared as a 1-D array here ({ext[0]}) but {expr} is an array of arrays; rewrite by hand"
    return ""


def directive_reason(text: str, sym: str) -> str:
    """Why a preprocessor use of SYM forbids the rewrite ('' when none): a macro NAMED sym, `#undef`/`#ifdef`/`defined()`
    of it, or a `#`/`##` operand in a macro body would become the member expression (`#define D_2.m`), invalid C
    (10-25 DATA-FRAME-O2: #define aliases rewritten into invalid code)."""
    tok = r"(?<![\w])%s\b" % re.escape(sym)
    code = mask(text)
    for m in re.finditer(r"^[ \t]*#[ \t]*(\w+)((?:\\\n|[^\n])*)", code, re.M):
        word, rest = m.group(1), m.group(2)
        if not re.search(tok, rest):
            continue
        line = m.group(0).strip().replace("\\\n", " ")
        if word in ("define", "undef") and re.match(r"\s*%s" % tok, rest):
            return f"{sym} is itself a macro name here ({line[:60]}): the member expression cannot replace it"
        if word in ("ifdef", "ifndef", "undef") or re.search(r"defined\s*\(?\s*%s" % tok, rest):
            return f"preprocessor test of {sym} here ({line[:60]})"
        if word == "define" and re.search(r"##\s*%s|%s\s*##|#\s*%s" % (tok, tok, tok), rest):
            return f"{sym} is a # or ## operand in a macro here ({line[:60]})"
    return ""


def type_tags(types: list[str]) -> set[str]:
    """The type names of declaration texts: `struct W *` -> {W}; qualifiers, pointers and dimensions dropped."""
    out: set[str] = set()
    for t in types:
        words = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", " ", t))
        out |= {
            w
            for w in words
            if w not in ("struct", "union", "enum", "const", "volatile", "unsigned", "signed", "static")
        }
    return out


def skip_reason(
    text: str,
    sym: str,
    owner: str,
    kinds_all: set[str],
    access: dict | None,
    expr: str = "",
    owner_types: list[str] | None = None,
) -> str:
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
    why = shape_reason(ext or [], expr) if expr else ""
    if why:
        return why
    o_ext, o_local = local_decls(text, owner)
    if o_local or (o_ext and cdecl.decl_kind(o_ext) - {"s"}):
        return f"owner {owner} is declared here as {(o_local or o_ext)[0]}, not as its struct"
    here = o_local + o_ext
    if owner_types and here and not (type_tags(here) & type_tags(owner_types)):
        return f"owner {owner} is declared here as a stand-in {here[0]}, the owner header declares {owner_types[0]}"
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
    owner_types: list[str] | None = None,
) -> tuple[str, str]:
    """(new text, skip reason): the per-file rewrite of rewrite.py. A file with a reason keeps its text. The safety
    rules run when the census MEMBER of the symbol is given (kinds every unit declares it with, ROM access, decl types)."""
    if sym not in text:
        return text, ""
    why = directive_reason(text, sym)  # census or not: `#define D_2.m` is invalid C
    if why:
        return text, why
    owner = re.match(r"\w+", expr).group(0)
    kinds = set(member.get("decl_kind") or []) if member is not None else None
    why = skip_reason(text, sym, owner, kinds, member.get("access"), expr, owner_types) if member is not None else ""
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
