# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow the boot frontier: run the boot; at a "(boot frontier)" stop link the unit that defines it
(its own callees become frontier stops through closure.py --frontier) and run again.

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


def boot(binary, rom, work):
    picture = work / "boot.ppm"
    picture.unlink(missing_ok=True)
    r = subprocess.run(["timeout", "120", str(binary), str(rom), str(picture), "180", "284", "222", "320", "240"],
                       env={**os.environ, "SDL_VIDEODRIVER": "offscreen"}, capture_output=True, text=True)
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
    args = parser.parse_args()
    if args.steps < 1 or args.jobs < 1:
        parser.error("--steps and --jobs must be positive")
    args.work.mkdir(parents=True, exist_ok=True)
    facts = closure.Facts(args.source, HERE.parent, args.shim_bindings,
                          closure.policy.game_code(json.loads(args.manifest.read_text())))

    def run_closure(why_unit):
        c = subprocess.run([sys.executable, "-I", str(HERE / "closure.py"), "--frontier", "--rounds", "6",
                            "--build", str(args.build), "--source", str(args.source), "--work", str(args.work),
                            "--manifest", str(args.manifest), "--shim-bindings", str(args.shim_bindings),
                            "--jobs", str(args.jobs), *(["--worklist", str(args.worklist)] if args.worklist else [])],
                           capture_output=True, text=True)
        (args.work / "advance-closure.log").write_text(c.stdout + c.stderr)
        if c.returncode != 0 or "closure: links" not in c.stdout.splitlines():
            print("halt: closure did not link after", why_unit, c.stdout.strip().splitlines()[-1:], flush=True)
            return False
        return True

    def halt(symbol, rc):
        needsfeed.write(args.work, args.manifest, args.worklist, symbol)
        return rc

    for step in range(args.steps):
        rc, last = boot(args.build / "rage_decomp_boot", args.rom, args.work)
        m = re.fullmatch(r"boot-missing: (\S+)", last)
        d = json.loads(args.manifest.read_text())
        print(f"step {step}: rc={rc} {last} :: {d['stops'].get(m.group(1), '') if m else ''}", flush=True)
        if rc == 0 and not m:
            return halt(None, 0)
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
