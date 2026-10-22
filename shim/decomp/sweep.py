# SPDX-License-Identifier: GPL-3.0-or-later
"""Check every function of a list independently against the native port: does its unit port-compile, is its
body C (not an INCLUDE_ASM stop), and does its link closure resolve (callees, data, shim rows).

One TSV row per function: symbol, order, check, verdict, first error line (then unit, linked, blockers, rest).
`check` is the first failing stage: unit-compile, body, callee (a direct reference does not resolve) or
closure (a deeper one does); `ok` when every stage passes. A libultra function (pinned SDK address or SDK unit)
never links natively: its row is `ok`/`shim` when the shim binds it, `ok`/`shim-internal` when no game source or
non-SDK header names it (only SDK bodies call it), else `sdk` with the shim class; walks stop there.
`verdict` is the cause class, for example decl-conflict, asm-only, callee-asm-stop, shim-row-missing, data-missing, no-definition.
Behaviour against the oracle (a host-built body replayed on the oracle's inputs) is not checked here: `decomp behave`
runs a MIPS candidate only, so the column `behave` says `gap`.

The list is a work queue in boot order: `--jobs` workers each take the next function, build only the units that
function reaches (each unit once, cached as a small graph: defined symbols, references per symbol section, stops;
objects are deleted at once), walk its closure with closure.py's own routing rules for what no unit defines (shim
bindings, pinned data addresses, aliases, stops), and the row is appended to sweep.tsv in boot order as soon as it
is done. A unit is found through an index of the identifiers the sources define at column 0."""
import argparse
import collections
import concurrent.futures
import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))

FLAGS = ["-g", "-m32", "-std=gnu89", "-fno-strict-aliasing", "-fwrapv", "-msse2", "-mfpmath=sse",
         "-ffunction-sections", "-fdata-sections"]
SECTION = re.compile(r"^(?:text|data(?:\.rel(?:\.ro)?(?:\.local)?)?|rodata|bss|sdata|sbss)\.(.+)$")
RELOC_HEAD = re.compile(r"^Relocation section '\.rel(\.[^']+)' ")
RELOC_ROW = re.compile(r"^\s*[0-9a-f]+\s+[0-9a-f]+\s+R_\w+\s+[0-9a-f]+\s+(\S+)")
NM_ROW = re.compile(r"^(?:[0-9a-f]+ )?([A-Za-z]) (\S+)$")
IGNORED = ("_GLOBAL_OFFSET_TABLE_", "__x86.", "__stack_chk")


def parse_relocs(text):
    """readelf -rW text -> {symbol: sorted referenced names} for every per-symbol section (-ffunction/-fdata-sections)."""
    edges, current = {}, None
    for line in text.splitlines():
        head = RELOC_HEAD.match(line)
        if head:
            m = SECTION.match(head.group(1)[1:])
            current = m.group(1) if m else None
            if current:
                edges.setdefault(current, set())
            continue
        row = RELOC_ROW.match(line)
        if current and row and not row.group(1).startswith(IGNORED) and not row.group(1).startswith("."):
            edges[current].add(row.group(1))
    return {k: sorted(v) for k, v in edges.items()}


def parse_nm(text):
    """nm text -> (defined {name: letter}, undefined set)."""
    defined, undefined = {}, set()
    for line in text.splitlines():
        m = NM_ROW.match(line.strip())
        if not m:
            continue
        if m.group(1) == "U":
            undefined.add(m.group(2))
        elif m.group(1) not in "aA":
            defined[m.group(2)] = m.group(1)
    return defined, undefined


def classify_error(line):
    """The cause class of one compiler diagnostic."""
    low = line.lower()
    for key, name in (("conflicting types", "decl-conflict"), ("redeclared", "decl-conflict"), ("redefinition", "decl-conflict"),
                      ("previous declaration", "decl-conflict"), ("conflicting", "decl-conflict"),
                      ("undeclared", "undeclared-ident"), ("unknown type name", "unknown-type"),
                      ("too many arguments", "arg-count"), ("too few arguments", "arg-count"),
                      ("no such file", "missing-include"), ("incompatible", "type-error"),
                      ("invalid", "type-error"), ("syntax error", "syntax-error")):
        if key in low:
            return name
    return "compile-other"


def first_error(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "error:" in line:
            return line
    return lines[-1] if lines else "no output"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False, **kw)


def build_unit(rw, unit, work, python="python3"):
    """Port-compile one unit and return its graph record; every intermediate file is removed."""
    stem = work / unit.replace("/", "__")
    pp = ["-DNON_MATCHING", "-DVERSION_US_REV1", "-D_LANGUAGE_C", "-DF3DEX_GBI_2", "-include",
          str(HERE / "native.h"), f"-I{rw}/include", f"-I{rw}/src/sdk/libgcc"]
    files = {s: str(stem) + s for s in (".i", ".stubs.c", ".json", ".d", ".o")}
    rec = {"unit": unit, "ok": False}
    try:
        p = run([python, "-I", str(HERE / "prepare.py"), "--compiler", "gcc", "--source", f"{rw}/{unit}",
                 "--output", files[".i"], "--stubs", files[".stubs.c"], "--inventory", files[".json"],
                 "--depfile", files[".d"], "--", *FLAGS, *pp])
        if p.returncode:
            rec["error"] = first_error(p.stderr)
            return rec
        inv = json.loads(pathlib.Path(files[".json"]).read_text())
        rec["mode"], rec["missing"] = inv["mode"], inv["missing"]
        rec["nm_errors"] = inv.get("non_matching_errors", [])[:3]
        g = run(["gcc", *FLAGS, "-x", "cpp-output", "-c", files[".i"], "-o", files[".o"]])
        if g.returncode:
            rec["error"] = first_error(g.stderr)
            return rec
        defined, _ = parse_nm(run(["nm", files[".o"]]).stdout)
        for name in rec["missing"]:  # the generated stops define these in the real build
            defined[name] = "T"
        rec["defs"] = defined
        rec["edges"] = parse_relocs(run(["readelf", "-rW", files[".o"]]).stdout)
        rec["ok"] = True
    except (OSError, ValueError) as exc:
        rec["error"] = f"sweep: {exc}"
    finally:
        for path in files.values():
            try:
                os.unlink(path)
            except OSError:
                pass
    return rec


def host_symbols():
    """Names the host libc/libm/libgcc of the 32-bit link provide (the closure never routes these)."""
    names = set()
    for lib in ("libc.so.6", "libm.so.6", "libgcc.a", "libgcc_s.so.1"):
        path = run(["gcc", "-m32", f"-print-file-name={lib}"]).stdout.strip()
        if os.path.isabs(path) and os.path.exists(path):
            flags = ["-g"] if lib.endswith(".a") else ["-D"]
            out = run(["nm", *flags, "--defined-only", path]).stdout
            names.update(m.group(2).split("@")[0] for line in out.splitlines() if (m := NM_ROW.match(line.strip())))
    return names


def stop_class(why):
    if "shim lacks" in why:
        return "shim-row-missing", why
    if "not identified for the shim" in why:
        return "shim-unidentified", why
    if "SDK" in why:
        return "shim-decision", why
    if "RW NEEDS" in why:
        return "callee-unit-uncompilable", why
    if "no C definition" in why:
        return "no-definition", why
    return "stop-other", why


class Router:
    """What no compiled unit defines, and original libultra: closure.py's routing against a copy of the port manifest.
    external(sym) -> (status, class, detail); status ok or blocked. sdk(sym, unit) says whether the port binds sym
    to the shim instead of linking its C body (pinned libultra address or an SDK unit), as closure.py does."""
    def __init__(self, facts, manifest, deny, host, route, rebind=None):
        self.facts, self.d, self.deny, self.host, self.route = facts, copy.deepcopy(manifest), deny, host, route
        self.pin = manifest.get("source_pin", "")[:7]
        self.cache, self.refs = {}, None
        if rebind:
            rebind(self.d, facts)  # stops the shim now provides, as the next closure round binds them

    def sdk(self, sym, unit=None):
        return bool(self.facts.sdk(sym) or (unit in self.facts.text and self.facts.sdk_unit(unit)))

    def internal(self, sym):
        """True when no game source or non-SDK header uses sym: only original SDK bodies call it, they never link
        natively, so the shim that replaces its callers covers it and it needs no row of its own. A prototype or
        extern declaration (TYPES.H declaring an SDK function) is not a use: only calls, address-taking and macro
        bodies count."""
        if self.refs is None:
            self.refs = set()
            texts = (t for u, t in self.facts.text.items() if not self.facts.sdk_unit(u))
            inc = self.facts.source / "include"
            heads = (p.read_text(errors="replace") for p in sorted(inc.rglob("*"))
                     if p.is_file() and "sdk" not in (q.lower() for q in p.relative_to(inc).parts))
            for text in (*texts, *heads):
                self.refs.update(IDENT.findall(DECLARATION.sub(" ", COMMENT.sub(" ", text))))
        return sym not in self.refs

    def external(self, sym):
        if sym not in self.cache:
            self.cache[sym] = self._resolve(sym)
        return self.cache[sym]

    def _resolve(self, sym):
        d = self.d
        if sym in self.host:
            return "ok", "host", ""
        if sym in d["bindings"] or sym in d["data"] or sym in d.get("rom_offsets", {}):
            return "ok", "manifest", ""
        if sym in d["stops"]:
            return ("blocked", *stop_class(d["stops"][sym]))
        kind, detail = self.route(d, sym, self.facts, self.deny, self.pin, False, set())
        if kind == "need":
            return "blocked", "sdk-data" if "SDK data" in detail else "data-missing", detail
        if sym in d["stops"]:
            return ("blocked", *stop_class(d["stops"][sym]))
        if kind == "added":
            # The walk built that unit and its object does not define sym (a definition for another version only),
            # so it is a linked unit as far as routing goes: route again, as closure.py does once it is linked.
            d["sources"].append(detail)
            kind, detail = self.route(d, sym, self.facts, self.deny, self.pin, False, set())
            if kind == "need":
                return "blocked", "data-missing", detail
            if sym in d["stops"]:
                return ("blocked", *stop_class(d["stops"][sym]))
            if kind == "added":
                return "blocked", "unit-not-built", f"{detail} defines {sym} but did not build"
        return "ok", kind, detail


DEF_LINE = re.compile(r"^(?!extern\b|typedef\b|#|//|/\*|\s)(.*)$")
IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
# One function prototype or extern declaration statement: [extern] TYPE NAME(PARAMS); with a type word (or *)
# before the name, so a call statement (no type, or a keyword such as return) never matches.
DECLARATION = re.compile(r"^[ \t]*(?!(?:return|else|case|goto|do|sizeof)\b)(?:extern[ \t]+)?[A-Za-z_][\w \t*]*?"
                         r"(?<=[\s*])\**[A-Za-z_]\w*[ \t]*\([^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*\)[ \t]*;"
                         r"|^[ \t]*extern[ \t][^;{}()=]*;", re.MULTILINE)


def build_index(texts):
    """unit -> text mapping to {identifier: [units]} over column-0 non-extern lines and INCLUDE_ASM names."""
    index = {}
    for unit, text in texts.items():
        names = set()
        for line in text.splitlines():
            if line.startswith("INCLUDE_ASM") or (line[:1].isalpha() and DEF_LINE.match(line)):
                names.update(IDENT.findall(line))
        for name in names:
            index.setdefault(name, []).append(unit)
    return index


class Graph:
    """Units are built on demand by `build(unit)` and cached; `index` maps an identifier to the units that may define it."""
    def __init__(self, router, build, index, records=()):
        self.router, self.build, self.index = router, build, index
        self.rec, self.glob, self.tried = {}, {}, set()
        self.lock = threading.RLock()
        self.locks = collections.defaultdict(threading.Lock)
        for r in records:
            self.register(r)
            self.tried.add(r["unit"])

    def register(self, r):
        with self.lock:
            if not r["ok"]:
                return
            self.rec[r["unit"]] = r
            for name, letter in r["defs"].items():
                if letter.isupper():
                    self.glob.setdefault(name, r["unit"])

    def get(self, unit):
        """The graph record of a unit, building it once; None when it does not compile."""
        with self.locks[unit]:
            if unit not in self.tried:
                r = self.build(unit)
                self.register(r)
                self.tried.add(unit)
            return self.rec.get(unit)

    def lookup(self, sym, unit):
        r = self.rec.get(unit)
        if r and sym in r["defs"]:
            return unit
        if sym in self.glob:
            return self.glob[sym]
        for cand in self.index.get(sym, ()):
            if cand not in self.tried:
                self.get(cand)
        return self.glob.get(sym)

    def blockers(self, unit, sym):
        """Breadth-first walk of everything sym reaches. -> [(depth, class, symbol, detail)], nearest first."""
        seen, found, queue = {(unit, sym)}, [], collections.deque([(unit, sym, 0)])
        while queue:
            u, s, depth = queue.popleft()
            r = self.rec[u]
            if s in r["missing"]:
                if depth:
                    found.append((depth, "callee-asm-stop", s, f"{s} is INCLUDE_ASM in {u}"))
                continue
            for target in r["edges"].get(s, ()):
                # libultra never links natively: the shim binding rule decides it, its C body is not walked
                home = None if self.router.sdk(target) else self.lookup(target, u)
                if home and not self.router.sdk(target, home):
                    if (home, target) not in seen:
                        seen.add((home, target))
                        queue.append((home, target, depth + 1))
                    continue
                with self.lock:
                    status, cls, detail = self.router.external(target)
                if status == "blocked":
                    found.append((depth + 1, cls, target, detail))
        found.sort(key=lambda b: b[0])
        dedup, keys = [], set()
        for b in found:
            if (b[1], b[2]) not in keys:
                keys.add((b[1], b[2]))
                dedup.append(b)
        return dedup


def owner(index, sym, unit, definers=None):
    """The unit that defines sym in the swept tree: the listed one while it still does, else the one that defines it
    now (a list made at an older pin names files that have moved or been split since). The index alone also holds
    column-0 prototypes, so `definers` (closure.Facts.definers: a body or INCLUDE_ASM) picks among its units."""
    units = index.get(sym, [])
    if unit in units:
        return unit
    found = definers(sym) if definers else units
    return found[0] if found else unit


def check_function(graph, deny, sym, unit):
    """-> (check, verdict, first error line, blocker count, further blockers)."""
    if graph.router.sdk(sym, None if unit in (None, "", "-") else unit):
        with graph.lock:
            status, cls, detail = graph.router.external(sym)
            internal = status != "ok" and graph.router.internal(sym)
        if status == "ok":
            return "ok", "shim", "", 0, ""  # the shim runs it; the original body never links
        if internal:
            return "ok", "shim-internal", f"{sym}: only original SDK bodies call it; their shim covers it", 0, ""
        return "sdk", cls, f"{sym}: {detail}"[:300], 1, ""
    if not unit or unit == "-":
        return "unit-compile", "no-owning-file", "no owning file in the list", 0, ""
    r = graph.get(unit)
    if r is None:
        err = deny.get(unit, f"{unit} not in the tree")
        return "unit-compile", classify_error(err) if unit in deny else "unit-missing", err, 0, ""
    if sym in r["missing"]:
        nm = r["nm_errors"][0] if r.get("nm_errors") else ""
        why = f"{sym} is INCLUDE_ASM in {unit}" + (f"; NON_MATCHING C rejected: {nm}" if nm else "")
        return "body", "asm-only-nm-rejected" if nm else "asm-only", why, 0, ""
    if sym not in r["defs"]:
        return "body", "not-in-object", f"{sym} is not defined by {unit} at this configuration", 0, ""
    bl = graph.blockers(unit, sym)
    if not bl:
        return "ok", "ok", "", 0, ""
    depth, cls, name, detail = bl[0]
    return ("callee" if depth == 1 else "closure"), cls, f"{name}: {detail}"[:300], len(bl), \
        ";".join(f"{b[1]}:{b[2]}" for b in bl[1:6])


def read_list(path):
    rows = []
    for line in pathlib.Path(path).read_text().splitlines():
        f = line.split("\t")
        if len(f) >= 5 and f[0].isdigit():
            rows.append({"order": int(f[0]), "symbol": f[1], "unit": f[4]})
    return rows


def stream(rows, work, workers, emit):
    """Run work(row) over the rows on `workers` threads in list order; emit(row, result) is called in list order the
    moment each result is ready, so early rows appear while later ones still run."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for row, fut in zip(rows, futures, strict=True):
            emit(row, fut.result())


def jobs_now():
    try:
        p = run([os.path.expanduser("~/.config/m2/admit"), "1.0"])
        if p.returncode == 0 and p.stdout.strip().isdigit():
            return max(1, int(p.stdout.strip()))
    except OSError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def summarize(rows):
    """(check, verdict) -> count, first order, first symbol; ordered by first boot occurrence."""
    groups = {}
    for r in rows:
        g = groups.setdefault((r["check"], r["verdict"]), [0, r["order"], r["symbol"]])
        g[0] += 1
        if r["order"] < g[1]:
            g[1], g[2] = r["order"], r["symbol"]
    return sorted(((k, *v) for k, v in groups.items()), key=lambda t: t[2])


def event(actor, status, message):
    try:
        run(["decomp", "event", "append", "--actor", actor, "--status", status, "--message", message])
    except OSError:
        pass


HEADER = "symbol\torder\tcheck\tverdict\tfirst_error\tunit\tlinked\tblockers\tmore\tbehave\n"


def line_of(r, linked):
    return "\t".join([r["symbol"], str(r["order"]), r["check"], r["verdict"], r["why"].replace("\t", " "),
                      r["unit"], "yes" if r["unit"] in linked else "no", str(r["count"]), r["more"], "gap"]) + "\n"


def run_sweep(rows, graph, deny, linked, workers, out, on_row=None):
    """Check every row, appending to out/sweep.tsv in list order as results arrive. -> the result rows."""
    results = []
    out.mkdir(parents=True, exist_ok=True)

    def work(row):
        facts = getattr(graph.router, "facts", None)
        row = {**row, "unit": owner(graph.index, row["symbol"], row["unit"], facts.definers if facts else None)}
        check, verdict, why, count, more = check_function(graph, deny, row["symbol"], row["unit"])
        return {**row, "check": check, "verdict": verdict, "why": why, "count": count, "more": more}

    with (out / "sweep.tsv").open("w") as fh:
        fh.write(HEADER)
        fh.flush()

        def emit(_row, result):
            results.append(result)
            fh.write(line_of(result, linked))
            fh.flush()
            if on_row:
                on_row(results)

        stream(rows, work, workers, emit)
    with (out / "summary.tsv").open("w") as fh:
        fh.write("check\tverdict\tcount\tfirst_order\tfirst_symbol\n")
        for (check, verdict), n, order, sym in summarize(results):
            fh.write(f"{check}\t{verdict}\t{n}\t{order}\t{sym}\n")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", required=True, type=pathlib.Path, help="TSV: order, symbol, port, decomp, owning file, block, percent")
    ap.add_argument("--rw", required=True, type=pathlib.Path, help="the RW (ragewars-decomp) tree to read")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--manifest", type=pathlib.Path, default=HERE / "us_rev1.json")
    ap.add_argument("--shim-bindings", type=pathlib.Path, default=HERE / "shim-bindings.txt")
    ap.add_argument("--jobs", type=int, default=0, help="default: what ~/.config/m2/admit allows")
    ap.add_argument("--keep", action="store_true", help="keep the per-unit graph records")
    ap.add_argument("--actor", default="", help="announce start and end on the event feed under this name")
    args = ap.parse_args()
    import closure
    rw, out = args.rw.resolve(), args.out.resolve()
    rows = read_list(args.list)
    jobs = args.jobs or jobs_now()
    work, graphs = out / "work", out / "graph"
    for d in (work, graphs):
        d.mkdir(parents=True, exist_ok=True)
    deny = {}

    def build(unit):
        cache = graphs / (unit.replace("/", "__") + ".json")
        rec = json.loads(cache.read_text()) if cache.exists() else build_unit(rw, unit, work)
        cache.write_text(json.dumps(rec))
        if not rec["ok"]:
            deny[unit] = f"{unit}: {rec.get('error', 'does not compile')}"
        return rec

    manifest = json.loads(args.manifest.read_text())
    facts = closure.Facts(rw, HERE.parent, args.shim_bindings, closure.policy.game_code(manifest))
    graph = Graph(Router(facts, manifest, deny, host_symbols(), closure.route, closure.rebind), build, build_index(facts.text))
    if args.actor:
        event(args.actor, "started", f"taste sweep: {len(rows)} functions, {jobs} jobs")
    results = run_sweep(rows, graph, deny, set(manifest["sources"]), jobs, out)
    ok = sum(r["check"] == "ok" for r in results)
    message = f"taste sweep: {ok}/{len(results)} functions pass; table {out / 'summary.tsv'}"
    print(message)
    if args.actor:
        event(args.actor, "passed", message)
    shutil.rmtree(work, ignore_errors=True)
    if not args.keep:
        shutil.rmtree(graphs, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
