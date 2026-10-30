# SPDX-License-Identifier: GPL-3.0-or-later
"""Link-all for rage_decomp_boot: the boot executable links the closure objects plus an archive of every
compiling non-SDK unit, so the linker pulls referenced units itself, and what no unit defines becomes a generated
run-on stop instead of a link failure. A unit outside the closure compiles through unitstep.py.
  plan      list the units the archive may hold (not the closure, not the data image, not libultra)
  archive   archive every non-empty object of the plan; list the members, and the members plus the closure objects
            (the image generator reads that list: a symbol an archived unit defines is not an undefined name)
  autogen   simulate the archive pull from the closure objects' undefined names and route every game name nothing
            defines (closure.route: libultra binding or stop, function stop, pinned data binding) into
            us_rev1.effective.json, the manifest the bindings, stops and trace table are generated from"""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hostabi  # noqa: E402
import portdir  # noqa: E402
HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
GAME_NAME = re.compile(r"(?:func|D)_[0-9A-Za-z_]+")
NM_LINE = re.compile(r"(.+?): (\S+) ([A-Za-z?-]) ")


def lines(path):
    return [line for line in pathlib.Path(path).read_text().splitlines() if line.strip()]


def plan(argv):
    parser = argparse.ArgumentParser(prog="plan")
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--image-units", type=pathlib.Path, required=True, help="file: one image unit per line")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    import closure
    import policy
    d = json.loads(args.manifest.read_text())
    facts = closure.Facts(args.source, HERE.parent, portdir.fact("shim-bindings.txt"), policy.game_code(d))
    skip = set(d["sources"]) | set(lines(args.image_units))
    eligible = sorted(u for u in facts.text if u not in skip and not facts.sdk_unit(u))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(u + "\n" for u in eligible))
    return 0


def archive(argv):
    parser = argparse.ArgumentParser(prog="archive")
    parser.add_argument("--plan", type=pathlib.Path, required=True)
    parser.add_argument("--objdir", type=pathlib.Path, required=True, help="the build's decomp/ directory")
    parser.add_argument("--closure-objects", type=pathlib.Path, required=True, help="linked-objects.txt")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--members", type=pathlib.Path, required=True)
    parser.add_argument("--linked", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    members = []
    for relative in lines(args.plan):
        for suffix in (".o", ".stubs.o"):
            path = args.objdir / (relative + suffix)
            if path.exists() and path.stat().st_size:
                members.append(str(path))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(args.output.name + ".new")
    temp.unlink(missing_ok=True)
    for start in range(0, len(members), 400):
        subprocess.run([hostabi.AR, "qcD", str(temp), *members[start:start + 400]], check=True)
    if not members:
        subprocess.run([hostabi.AR, "qcD", str(temp)], check=True)
    subprocess.run([hostabi.RANLIB, "-D", str(temp)], check=True)
    # An unchanged archive keeps its timestamp, so the boot executable is not relinked for nothing.
    if args.output.exists() and hashlib.sha256(args.output.read_bytes()).digest() == \
            hashlib.sha256(temp.read_bytes()).digest():
        temp.unlink()
    else:
        temp.replace(args.output)
    args.members.write_text("".join(m + "\n" for m in members))
    args.linked.write_text("".join(o + "\n" for o in lines(args.closure_objects) + members))
    return 0


def symbols(paths):
    """{path: (defined names, undefined names)} of global symbols, one nm run per few hundred objects."""
    result = {str(p): (set(), set()) for p in paths}
    for start in range(0, len(paths), 400):
        r = subprocess.run([hostabi.NM, "-g", "-A", "-P", *map(str, paths[start:start + 400])],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            m = NM_LINE.match(line)
            if m and m.group(1) in result:
                if m.group(3) == "U":
                    result[m.group(1)][1].add(hostabi.strip(m.group(2)))
                elif m.group(3) not in "uUN-?":
                    result[m.group(1)][0].add(hostabi.strip(m.group(2)))
    return result


def library_names(libs):
    """Global names the platform libraries define: the linker resolves them there, so they are never routed."""
    names = set()
    for lib in libs:
        r = subprocess.run([hostabi.NM, "-g", "--defined-only", "-P", lib], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and not line.endswith(":"):
                names.add(hostabi.strip(fields[0]))
    return names


def pull(seeds, have, defined_by):
    """The archive members the linker pulls for the undefined names seeds, and the names left undefined.
    defined_by maps a name to the member that defines it; a member's own undefined names join the queue."""
    pulled, unresolved, queue, seen = [], set(), sorted(seeds), set()
    while queue:
        name = queue.pop()
        if name in seen or name in have:
            continue
        seen.add(name)
        member = defined_by.get(name)
        if member is None:
            unresolved.add(name)
        elif member[0] not in pulled:
            pulled.append(member[0])
            have |= member[1]
            queue.extend(member[2])
    return pulled, unresolved


def autogen(argv):
    parser = argparse.ArgumentParser(prog="autogen")
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--boot", type=pathlib.Path, required=True, help="the boot build's decomp-boot directory")
    parser.add_argument("--objdir", type=pathlib.Path, required=True, help="the build's decomp/ directory")
    parser.add_argument("--plan", type=pathlib.Path, required=True)
    parser.add_argument("--members", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--libs", nargs="*", default=[], help="platform libraries (n64compat ...): names they define")
    args = parser.parse_args(argv)
    import closure
    import policy
    d = json.loads(args.manifest.read_text())
    facts = closure.Facts(args.source, HERE.parent, portdir.fact("shim-bindings.txt"), policy.game_code(d))
    boot_objects = lines(args.boot / "linked-objects.txt")
    members = lines(args.members)
    image_objects = lines(args.boot / "image-objects.txt")
    syms = symbols([pathlib.Path(p) for p in boot_objects + members + image_objects])
    have = set(d["data"]) | set(d["bindings"]) | set(d["stops"]) | set(d.get("rom_offsets", {}))
    have |= {hostabi.strip(m.group(1)) for m in re.finditer(r"^\s*(\w+)\s*=", (args.boot / "image.ld").read_text(), re.M)}
    have |= set(json.loads((args.boot / "image.json").read_text()).get("function_stops", []))
    have |= library_names(args.libs)
    seeds = {d["entry"], *d["bindings"].values()}
    for path in boot_objects:
        have |= syms[path][0]
        seeds |= syms[path][1]
    # image.c is an executable source, not a closure object: its pointer words name the functions they hold.
    image_c = args.boot / "image.c"
    if image_c.exists():
        seeds |= {hostabi.strip(name) for name in re.findall(r"\.long (\w+)", image_c.read_text())}
    # An image unit's data is defined by the image (image.ld), not by an object.
    defined_by = {}
    for path in members:
        for name in syms[path][0]:
            defined_by.setdefault(name, (path, syms[path][0], syms[path][1]))
    # A committed stop is a placeholder for a body not linked yet: a member that really defines it replaces it,
    # so the name stays pullable (and the stop is dropped below) instead of colliding with the archive member.
    placeholders = set(d["stops"]) & set(defined_by)
    have -= placeholders
    pulled, unresolved = pull(seeds - have, have, defined_by)
    # Units whose compile failed (the unit step's .failed marker) are refused with its first error.
    deny = {}
    for relative in lines(args.plan):
        failed = args.objdir / (relative + ".failed")
        if failed.exists():
            deny[relative] = failed.read_text().strip()
    pin = d["source_pin"][:7]
    trial, fresh, report = json.loads(json.dumps(d)), set(), {"routed": {}, "skipped": {}, "needs": []}
    routed_names, targets = set(), set(d["bindings"].values())
    # An image unit is linked as data: a name it defines only for another version (#if VERSION_DE) has no C
    # definition at this pin and binds to its pinned address like any other undefined data.
    image_list = args.plan.with_name("image-units.txt")
    linked_sources = d["sources"] + [u for u in (lines(image_list) if image_list.exists() else []) if u not in d["sources"]]
    trial["sources"] = linked_sources
    while unresolved - routed_names:
        todo = sorted(unresolved - routed_names)
        routed_names |= set(todo)
        facts.prime(n for n in todo if n in facts.addrs or GAME_NAME.fullmatch(n))
        for name in todo:
            if not (name in facts.addrs or GAME_NAME.fullmatch(name)):
                continue  # a library name (n64compat, libc, libgcc): the linker resolves it
            kind, detail = closure.route(trial, name, facts, deny, pin, False, fresh)
            if kind == "need":
                report["needs"].append(detail)
            elif kind == "added":
                report["skipped"][name] = detail  # its unit is not in the archive: closure.py still routes the link error
                trial["sources"] = linked_sources
            elif kind == "routed":
                report["routed"][name] = detail
        # A routed alias binds a data name to a function the linker pulls (EXTERN): that member's own undefined
        # names need routing too, so pull again from the new targets until nothing new is reached.
        new_targets = set(trial["bindings"].values()) - targets
        targets |= new_targets
        more, left = pull(new_targets - have, have, defined_by)
        pulled += [m for m in more if m not in pulled]
        unresolved |= left
    # An alias binding routes its target as undefined when the unit that defines it is already linked: drop the
    # stop a linked or pulled object defines (it would be a multiple definition).
    defined = set().union(*(syms[p][0] for p in boot_objects), *(defined_by[n][1] for n in defined_by if defined_by[n][0] in pulled))
    for name in [n for n in trial["stops"] if n in defined]:
        del trial["stops"][name]
        report["routed"].pop(name, None)
    trial["sources"] = d["sources"]
    # Data nothing defines and no pin addresses (a pulled unit reads it): zeroed weak storage, so the link holds and
    # the unit that really defines it wins when the closure links it. Listed as data needs in the report.
    stubs = sorted(n.split(" | ")[0] for n in report["needs"])
    code = ["/* Generated: weak zeroed storage for data no unit defines and the pin does not address. */"]
    # (A COFF weak definition is a weak external whose default section --gc-sections drops, leaving the reference undefined:
    # on Windows the storage is plain. Nothing else defines the name, so nothing can collide with it.)
    attributes = "aligned(16)" if hostabi.COFF else "weak, aligned(16)"
    code += [f"__attribute__(({attributes})) char {n}[4096];" for n in stubs]
    if not stubs:
        code.append("typedef void n64c_autodata_translation_unit;")
    args.output.with_name("autodata.c").write_text("\n".join(code) + "\n")
    args.output.write_text(json.dumps(trial, indent=2, sort_keys=True) + "\n")
    report.update(pulled=len(pulled), members=len(members), refused=deny)
    args.output.with_suffix(".report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(f"link-all: {len(pulled)}/{len(members)} archive units pulled, {len(report['routed'])} names routed "
          f"({sum(1 for v in report['routed'].values() if v.startswith('stop') or v.startswith('frontier'))} stops), "
          f"{len(report['needs'])} data needs, {len(report['skipped'])} unrouted, {len(deny)} units refused")
    return 0


def main():
    commands = {"plan": plan, "archive": archive, "autogen": autogen}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    return commands[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
