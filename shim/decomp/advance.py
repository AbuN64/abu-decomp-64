# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow the boot frontier: run the boot; at a "(boot frontier)" stop link the unit that defines it
(its own callees become frontier stops through closure.py --frontier) and run again.

Each boot is the run-on diagnostic boot (N64C_RUN_ON=1, decomp/runon.c): it does not end at the first failure but
lists every unlinked call, shim stop, fault, hang and uncovered byte-order range in boot order (boot-diag: lines).
One step links every frontier unit that list names. When none is left the walk halts and WORK/boot-diag.jsonl holds
the whole list, each row routed to its owner class; needs.jsonl gets the same rows. --fail-fast walks one stop per
boot as before (a build without run-on prints no boot-diag: lines and is walked that way too).

Halts at a stop that is not a frontier stop (compile conflict, no C body, libultra), at a frontier unit in
the original libultra (it needs a shim binding decision, never a native link), or at any other boot
result. One line per step on stdout; the last boot's stderr is WORK/boot.log."""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import closure  # noqa: E402
import needs as needsfeed  # noqa: E402


def next_unit(d, symbol, facts):
    """The unit to link for a boot stop at symbol, or (None, why the walk halts)."""
    why = d["stops"].get(symbol, "")
    if (address := facts.sdk(symbol)):
        return None, f"libultra 0x{address:08X} needs a shim binding decision: {symbol}"
    if "(boot frontier)" not in why:
        return None, "not a frontier stop: " + (why or "INCLUDE_ASM or unlisted stop in a linked unit")
    unit = why.split(" ", 1)[0]
    if "/sdk/" in unit.lower() or not unit.startswith("src/"):
        names = closure.policy.functions(facts.text.get(unit, ""))
        if unit in facts.text and not any(facts.sdk(n) for n in names):
            # A unit the decomp files under SDK whose functions all sit outside the libultra range (the game
            # scheduler cluster, controller pak): the shim has no libultra name to bind. The owner is the decomp.
            return None, ("SDK-directory unit holds no libultra-range function: " + unit + " (" + ", ".join(names[:4]) +
                          "); decomp must move the game functions (e.g. " + symbol + ") into a non-SDK unit "
                          "and name the osSc* ones the shim must provide (SDK: shim binding decision)")
        return None, "libultra frontier needs a shim binding decision: " + unit
    return unit, ""


DIAG = re.compile(r"boot-diag: (missing|stop|code-pointer|fault|layout|hang) (\S+) at=(\d+)/(\d+) after=(\S+) "
                  r"in=(\S+)(?: (.*))?")
# Who acts on each run-on failure class (FL-OPUS-1 classes): the decomp/shim lanes for unlinked calls (by their
# manifest stop text, as needs.py classifies it), the byte-order lane for data that reaches the game big-endian,
# the runtime walker for everything only a run shows.
OWNER = {"layout": "byte-order", "code-pointer": "address-model", "fault": "runtime", "hang": "runtime"}


def diag_rows(text, project):
    """The run-on failures of one boot's stderr, in boot order, each with its owner class."""
    rows = []
    for line in text.splitlines():
        m = DIAG.fullmatch(line)
        if not m:
            continue
        cls, subject = m.group(1), m.group(2)
        if cls == "missing":
            why = project.get("stops", {}).get(subject, "")
            got = needsfeed.classify(why) if why else ("unlisted", needsfeed.unit_of(why) or "", "", "")
            owner = got[0] if got else "frontier"
        elif cls == "stop":
            owner = "byte-order" if subject.startswith(("rnc", "rnc2")) else \
                "address-model" if subject.startswith(("MMIO-", "TLB-miss", "code-pointer")) else "runtime"
        else:
            owner = OWNER[cls]
        rows.append({"boot_order": len(rows) + 1, "class": cls, "subject": subject, "owner": owner,
                     "census": int(m.group(3)), "census_total": int(m.group(4)), "after": m.group(5),
                     "in": m.group(6), "details": m.group(7) or ""})
    return rows


def boot(binary, rom, work, run_on=False, seconds=120):
    picture = work / "boot.ppm"
    picture.unlink(missing_ok=True)
    env = {**os.environ, "SDL_VIDEODRIVER": "offscreen"}
    if run_on:
        # The boot ends itself before the timeout, so the failure list and the census close the log.
        env.update({"N64C_RUN_ON": "1", "N64C_RUN_ON_LIMIT": str(max(seconds - 15, 10))})
    # -k: the boot catches SIGTERM (SDL/LUS handlers) and a hung boot kept running past it, stalling the walk.
    r = subprocess.run(["timeout", "-k", "10", str(seconds), str(binary), "--no-dialog", "--user-dir", str(work / "user"),
                        "--rom", str(rom), "--capture", str(picture), "--retrace", "180", "--fb", "284x222",
                        "--window", "320x240"], env=env, capture_output=True, text=True)
    (work / "boot.log").write_text(r.stderr)
    lines = [line for line in r.stderr.splitlines() if line.startswith("boot-")]
    stops = [line for line in lines if re.fullmatch(r"boot-missing: \S+", line)]
    return r.returncode, stops[-1] if stops else lines[-1] if lines else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--work", type=pathlib.Path, required=True)
    parser.add_argument("--rom", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, default=HERE / "us_rev1.json")
    parser.add_argument("--shim-bindings", type=pathlib.Path, default=HERE / "shim-bindings.txt")
    parser.add_argument("--worklist", type=pathlib.Path, help="worklist-rw.tsv, for trace_order in needs.jsonl")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--seconds", type=int, default=120, help="time limit of one boot")
    parser.add_argument("--fail-fast", action="store_true", help="walk one stop per boot (no run-on boot)")
    args = parser.parse_args()
    if args.steps < 1 or args.jobs < 1:
        parser.error("--steps and --jobs must be positive")
    args.work.mkdir(parents=True, exist_ok=True)
    facts = closure.Facts(args.source, HERE.parent, args.shim_bindings,
                          closure.policy.game_code(json.loads(args.manifest.read_text())))

    def run_closure(why_unit):
        c = subprocess.run([sys.executable, "-I", str(HERE / "closure.py"), "--rounds", "6",
                            "--build", str(args.build), "--source", str(args.source), "--work", str(args.work),
                            "--manifest", str(args.manifest), "--shim-bindings", str(args.shim_bindings),
                            "--jobs", str(args.jobs), *(["--worklist", str(args.worklist)] if args.worklist else [])],
                           capture_output=True, text=True)
        (args.work / "advance-closure.log").write_text(c.stdout + c.stderr)
        if c.returncode != 0 or "closure: links" not in c.stdout.splitlines():
            print("halt: closure did not link after", why_unit, c.stdout.strip().splitlines()[-1:], flush=True)
            return False
        return True

    diag = []

    def halt(symbol, rc):
        needsfeed.write(args.work, closure.effective_manifest(args.build, args.manifest), args.worklist, symbol, diag)
        (args.work / "boot-diag.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in diag))
        return rc

    def run_on_step(d, rc):
        """One run-on boot: link every frontier unit its failure list names. None when the walk goes on, else rc."""
        owners = {}
        for r in diag:
            owners[r["owner"]] = owners.get(r["owner"], 0) + 1
        print("   run-on:", len(diag), "failures in boot order;",
              ", ".join(f"{k} {v}" for k, v in sorted(owners.items())), flush=True)
        missing = [r["subject"] for r in diag if r["class"] == "missing"]
        if set(missing) & set(closure.drop_moved(d, facts)):
            print("   dropped stale stops (their units moved)", flush=True)
            args.manifest.write_text(json.dumps(d, indent=2) + "\n")
            return None if run_closure("stale stops") else halt(None, 1)
        image = args.build / "decomp-boot" / "image.json"
        image_stops = json.loads(image.read_text()).get("function_stops", []) if image.exists() else []
        deny_path = args.work / "deny.json"
        deny = json.loads(deny_path.read_text()) if deny_path.exists() else {}
        moved = []
        for sym in missing:
            if sym in image_stops and sym not in d["stops"] and sym not in d["bindings"]:
                trial = json.loads(json.dumps(d))
                kind, detail = closure.route(trial, sym, facts, deny, d.get("source_pin", "")[:7], True, set())
                if kind == "added" or detail.startswith(("binding", "frontier")):
                    print(f"   routed image stop {sym}: {kind} {detail}", flush=True)
                    d = trial
                    moved.append(sym)
                continue
            unit, why = next_unit(d, sym, facts)
            if unit:
                del d["stops"][sym]
                unit in d["sources"] or d["sources"].append(unit)
                moved.append(sym)
        if moved:
            print(f"   linking {len(moved)} frontier stops: {' '.join(moved[:12])}{' ...' if len(moved) > 12 else ''}",
                  flush=True)
            args.manifest.write_text(json.dumps(d, indent=2) + "\n")
            return None if run_closure(f"{len(moved)} frontier stops") else halt(moved[0], 1)
        first = next((r for r in diag if r["owner"] != "frontier"), None)
        print("halt: no frontier stop left;", "first failure:" if first else "boot clean",
              f"{first['class']} {first['subject']} ({first['owner']})" if first else "", flush=True)
        return halt(first["subject"] if first and first["class"] == "missing" else None,
                    1 if rc in (124, 137) else 0)

    for step in range(args.steps):
        rc, last = boot(args.build / "ragewars", args.rom, args.work, not args.fail_fast, args.seconds)
        d = json.loads(args.manifest.read_text())
        log = args.work / "boot.log"
        text = log.read_text() if not args.fail_fast and log.exists() else ""
        diag = diag_rows(text, json.loads(closure.effective_manifest(args.build, args.manifest).read_text()))
        if diag:
            print(f"step {step}: rc={rc} {last}", flush=True)
            got = run_on_step(d, rc)
            if got is not None:
                return got
            continue
        m = re.fullmatch(r"boot-missing: (\S+)", last)
        print(f"step {step}: rc={rc} {last} :: {d['stops'].get(m.group(1), '') if m else ''}", flush=True)
        if rc == 0 and not m:
            return halt(None, 0)
        if rc in (124, 137):  # timeout: TERM honoured (124) or the kill after -k (137)
            print(f"halt: boot hung (no exit in 120 s) after {last}; see {args.work / 'boot.log'}", flush=True)
            return halt(None, 1)
        if rc != 78 or not m:
            print(f"halt: unexpected boot result rc={rc}; see {args.work / 'boot.log'}", flush=True)
            return halt(None, 1)
        if m.group(1) in closure.drop_moved(d, facts):
            # The stop named a unit the pinned tree no longer has: route the symbol again from the current tree.
            print(f"   dropped stale stop {m.group(1)} (its unit moved)", flush=True)
            args.manifest.write_text(json.dumps(d, indent=2) + "\n")
            if not run_closure("a stale stop"):
                return halt(m.group(1), 1)
            continue
        sym = m.group(1)
        image = args.build / "decomp-boot" / "image.json"
        image_stops = json.loads(image.read_text()).get("function_stops", []) if image.exists() else []
        if sym in image_stops and sym not in d["stops"] and sym not in d["bindings"]:
            # A stop no manifest row names: the data image's generated stop for a code pointer (image.py
            # function_stops, e.g. a KUSEG alias D_00XXXXXX in a callback table). It routes like an undefined link
            # symbol: an alias binds to its function, a function's unit is added or becomes a frontier stop.
            deny_path = args.work / "deny.json"
            deny = json.loads(deny_path.read_text()) if deny_path.exists() else {}
            trial = json.loads(json.dumps(d))
            kind, detail = closure.route(trial, sym, facts, deny, d.get("source_pin", "")[:7], True, set())
            # Only a binding, a linked unit or a frontier moves the walk; any other route result (a no-C stop) halts
            # below exactly as before, with the manifest untouched.
            if kind == "added" or detail.startswith(("binding", "frontier")):
                print(f"   routed image stop {sym}: {kind} {detail}", flush=True)
                args.manifest.write_text(json.dumps(trial, indent=2) + "\n")
                if not run_closure(sym):
                    return halt(sym, 1)
                continue
        unit, why = next_unit(d, m.group(1), facts)
        if not unit:
            print("halt:", why, flush=True)
            return halt(m.group(1), 0)
        del d["stops"][m.group(1)]
        unit in d["sources"] or d["sources"].append(unit)
        args.manifest.write_text(json.dumps(d, indent=2) + "\n")
        if not run_closure(unit):
            return halt(m.group(1), 1)
    return halt(None, 0)
if __name__ == "__main__":
    raise SystemExit(main())
