# SPDX-License-Identifier: GPL-3.0-or-later
"""The static byte-order gate: list the decomp blob facts of the manifest's version that nothing types.

Every blob fact (the source's blobs.toml) is typed by a blob_layouts row (by blob or format), a loads/resources row
over one of its ranges, or a byte_streams declaration of its format; any other blob is untyped, and the boot stops at
its first DMA (n64c_pi UNTYPED, from link.py). This lists them before a boot does: one BYTEORDER line (and, with
--needs, one JSON row) per untyped blob, the counts per class, and exit status 1 while any blob is untyped.
With --rom it also lists every RNC stream in the cartridge that no blob fact or row covers (UNFACTED: the blob fact
itself is missing, so nothing registers it; n64c_pi stops a DMA that starts on one)."""
import argparse
import collections
import json
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import link
import policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--needs", type=pathlib.Path, help="write one JSON row per untyped blob here")
    parser.add_argument("--rom", type=pathlib.Path, help="the cartridge: also list RNC streams no blob fact covers")
    args = parser.parse_args()
    project = policy.load_project(args.manifest, args.source)
    classes = link.byte_order(project, args.source)
    counts = collections.Counter((kind, fmt) for kind, _, _, fmt in classes.values())
    needs = []
    for blob, (kind, ranges, codec, fmt) in sorted(classes.items(), key=lambda pair: pair[1][1][0]):
        if kind == "untyped":
            size = sum(hi - lo for lo, hi in ranges)
            print("BYTEORDER", blob, f"0x{ranges[0][0]:x}", size, codec, fmt, sep="\t")
            needs.append({"kind": "byteorder", "blob": blob, "rom": f"0x{ranges[0][0]:x}", "bytes": size,
                          "codec": codec, "format": fmt})
    for at, method, unpacked, packed in (link.unfacted_streams(args.rom.read_bytes(), project, args.source)
                                         if args.rom else []):
        print("UNFACTED", f"0x{at:x}", f"rnc{method}", unpacked, packed, sep="\t")
        needs.append({"kind": "byteorder", "blob": None, "rom": f"0x{at:x}", "bytes": 18 + packed,
                      "codec": f"rnc{method}", "format": None, "unpacked": unpacked,
                      "need": "a blob fact (blobs.toml) for this RNC stream, then a blob_layouts row"})
        counts["unfacted", f"rnc{method}"] += 1
    for (kind, fmt), count in sorted(counts.items(), key=lambda pair: (pair[0][0], str(pair[0][1]))):
        print("COUNT", kind, count, fmt, sep="\t")
    if args.needs:
        args.needs.write_text("".join(json.dumps(row) + "\n" for row in needs))
    return 1 if needs else 0


if __name__ == "__main__":
    sys.exit(main())
