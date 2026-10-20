# SPDX-License-Identifier: GPL-3.0-or-later
"""Write WORK/needs.jsonl: the boot closure's open blockers as one machine-readable row each.
Row: {symbol, class, unit, file_line, first_error, pin, trace_order, halt}. Classes:
decl (RW needs one declaration), placement (game code filed under the SDK), body (no C definition),
nm-compile (the unit does not compile for another reason), data-type (data with no pinned address or
provider), library-name (a libultra function the shim has no identified name for). A "(boot frontier)"
stop is pending work of the walk, not a need, and is not written.
With a run-on boot's failure list (advance.py diag_rows), stop rows the boot reached carry boot_order, and every other
failure of that boot is a row too: class byte-order (bytes no byte-order row covers, RNC layouts), address-model (raw
guest code pointers, MMIO/TLB stops) or runtime (faults, hangs, refused tasks), trace_order its census position."""
import json
import pathlib
import re

SITE = re.compile(r"(src/\S+?\.(?:CPP|c)):(\d+)\s*(.*)", re.S)
UNIT = re.compile(r"src/\S+?\.(?:CPP|c)")
DECL = re.compile(r"conflicting types|redefinition|redeclared|unknown type name|undeclared|incompatible|"
                  r"has no member|NEEDS one declaration|previous declaration|storage class")


def unit_of(text):
    """The first source unit a stop or need text names, or None."""
    m = UNIT.search(text)
    return m.group(0) if m else None


def classify(why):
    """(class, unit, file_line, first_error) of one stop text, or None for a pending frontier stop."""
    if "(boot frontier)" in why:
        return None
    site = SITE.match(why)
    unit, file_line = (site.group(1), f"{site.group(1)}:{site.group(2)}") if site else (unit_of(why), "")
    error = re.sub(r"\s+at [0-9a-f]{7}\b.*$", "", site.group(3) if site else why, flags=re.S).strip()
    if "no libultra-range function" in why:
        kind = "placement"
    elif why.startswith("libultra") or "original SDK unit" in why or "SDK: shim binding decision" in why:
        kind = "library-name"
    elif site and DECL.search(why):
        kind = "decl"
    elif site:
        kind = "nm-compile"
    else:
        kind = "body"
    return kind, unit or "", file_line, error


def trace_orders(worklist):
    """symbol -> trace order from a worklist-rw.tsv (order, symbol, ...)."""
    if not worklist or not pathlib.Path(worklist).exists():
        return {}
    rows = [line.split("\t") for line in pathlib.Path(worklist).read_text().splitlines()[1:]]
    return {r[1]: int(r[0]) for r in rows if len(r) > 1 and r[0].isdigit()}


def rows(project, data_needs, pin, order, halt=None):
    out = []
    for symbol, why in sorted(project.get("stops", {}).items()):
        got = classify(why)
        if got:
            out.append({"symbol": symbol, "class": got[0], "unit": got[1], "file_line": got[2],
                        "first_error": got[3], "pin": pin, "trace_order": order.get(symbol), "halt": symbol == halt})
    for line in data_needs:
        symbol, _, why = line.partition(" | ")
        if symbol.strip():
            site = SITE.search(why)
            out.append({"symbol": symbol.strip(), "class": "data-type", "unit": unit_of(why) or "",
                        "file_line": f"{site.group(1)}:{site.group(2)}" if site else "", "first_error": why.strip(),
                        "pin": pin, "trace_order": None, "halt": False})
    return out


def diag_needs(got, diag, pin):
    """Merge one run-on boot's failure list into the need rows (in place)."""
    by_symbol = {r["symbol"]: r for r in got}
    for d in diag or []:
        if d["class"] == "missing" and d["subject"] in by_symbol:
            by_symbol[d["subject"]].setdefault("boot_order", d["boot_order"])
            continue
        if d["owner"] == "frontier":
            continue
        got.append({"symbol": d["subject"], "class": d["owner"] if d["class"] != "missing" else "unlisted",
                    "unit": "", "file_line": "", "first_error": f"{d['class']} {d['subject']} in={d['in']} {d['details']}".strip(),
                    "pin": pin, "trace_order": d["census"], "halt": False, "boot_order": d["boot_order"]})


def write(work, manifest, worklist=None, halt=None, diag=None):
    """Write work/needs.jsonl from the manifest's stops, work/data-needs.txt and a run-on boot's failure list;
    returns the row count."""
    work = pathlib.Path(work)
    project = json.loads(pathlib.Path(manifest).read_text())
    path = work / "data-needs.txt"
    data = path.read_text().splitlines() if path.exists() else []
    got = rows(project, data, project.get("source_pin", "")[:7], trace_orders(worklist), halt)
    diag_needs(got, diag, project.get("source_pin", "")[:7])
    (work / "needs.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in got))
    return len(got)
