# SPDX-License-Identifier: GPL-3.0-or-later
"""Preprocess original C and generate stops for its selected INCLUDE_ASMs."""
import argparse
import json
import pathlib
import re
import subprocess
import sys

IDENT = r"[A-Za-z_][A-Za-z_0-9]*"


def markers(source, prefix):
    # Match the complete declaration emitted by native.h, not comments,
    # strings, inactive version branches or arbitrary symbol substrings.
    clean, quote, escaped = [], None, False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif ord(char) == 92:
                escaped = True
            elif char == quote:
                quote = None
            clean.append(" ")
        elif char in (chr(34), chr(39)):
            quote = char
            clean.append(" ")
        else:
            clean.append(char)
    source = "".join(clean)
    kind = r"void\s+" if prefix == "missing" else r"char\s+"
    suffix = r"\s*\(\s*void\s*\)\s*;" if prefix == "missing" else r"\s*;"
    return sorted(set(re.findall(r"\bextern\s+" + kind +
        r"n64c_" + prefix + r"_marker_(" + IDENT + ")" + suffix, source)))


def errors(compiler, source, flags):
    result = subprocess.run([compiler, *flags, "-x", "c", "-fsyntax-only", str(source)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.returncode, [line for line in result.stderr.splitlines() if "error:" in line]
def prepare(compiler, source, output, stubs, inventory, depfile, flags):
    output.parent.mkdir(parents=True, exist_ok=True)
    # A unit whose fuzzy NON_MATCHING bodies do not compile still contributes its
    # matched C: it is rebuilt as the matching build sees it, and the fuzzy
    # functions become INCLUDE_ASM stops. Their diagnostics stay in the inventory.
    mode, rejected = "NON_MATCHING" if "-DNON_MATCHING" in flags else "matched", []
    if mode == "NON_MATCHING":
        status, rejected = errors(compiler, source, flags)
        matched = [flag for flag in flags if flag != "-DNON_MATCHING"]
        if status and not errors(compiler, source, matched)[0]:
            flags, mode = matched, "matched"
            sys.stderr.write(f"decomp-prepare: {source}: NON_MATCHING C does not compile; "
                             "using matched C with stops\n")
        else:
            rejected = []
    command = [compiler, *flags, "-x", "c", "-E", "-MD", "-MF", str(depfile),
               "-MT", str(output), str(source)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        return result.returncode
    missing = markers(result.stdout, "missing")
    data = markers(result.stdout, "data")
    # No definitions are fabricated for data macros. They remain unresolved
    # until the ROM loader has a qualified layout and relocation description.
    declarations = ["/* Generated missing-function stops; no original code. */",
        "extern long long n64c_boot_missing(const char *);"]
    declarations += ['long long ' + name + '(void) { return n64c_boot_missing("' + name + '"); }'
                     for name in missing]
    output.write_text(result.stdout)
    stubs.write_text("\n".join(declarations) + "\n")
    inventory.write_text(json.dumps({"source": str(source), "missing": missing,
                                    "data": data, "mode": mode,
                                    "non_matching_errors": rejected[:50]},
                                   sort_keys=True) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("compiler", "source", "output", "stubs", "inventory", "depfile"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("flags", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
    return prepare(args.compiler, pathlib.Path(args.source), pathlib.Path(args.output),
                   pathlib.Path(args.stubs), pathlib.Path(args.inventory),
                   pathlib.Path(args.depfile), flags)


if __name__ == "__main__":
    sys.exit(main())
