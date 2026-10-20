#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""CI check: the stream tile's exact argument set boots as far as the headless boot (closes the window-only class:
tile 75 vs run-on 252 on 2026-10-20). Both boots are run-on boots (N64C_RUN_ON=1, runon.c) of the same binary under
SDL's offscreen driver; the tile one uses TILE_ARGS, the headless one advance.py's arguments. Fails (exit 1) when the
tile census is below the headless census. With --seat it also fails (exit 2) when the seat profile's port command no
longer equals TILE_ARGS, so a changed tile cannot escape the check.
  python3 -I tile_parity.py --binary ragewars --rom ragewars.z64 [--seat seat.json] [--output parity.json]"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # the cycle snapshot refuses stray .pyc files

TILE_ARGS = ["--no-dialog", "--user-dir", "{user}", "--rom", "{rom}", "--fb", "284x222", "--window", "640x480"]
HEADLESS_ARGS = ["--no-dialog", "--user-dir", "{user}", "--rom", "{rom}", "--capture", "{picture}", "--retrace", "180",
                 "--fb", "284x222", "--window", "320x240"]
END = re.compile(r"boot-diag: end (\d+) failures \(.*?\), census (\d+)/(\d+)")
STEP = re.compile(r"boot-step: census (\d+)/(\d+) hit")


def census(stderr):
    """Distinct trace functions the boot hit: the run-on end line, else the last census step line, else 0."""
    end = None
    for end in END.finditer(stderr):
        pass
    if end:
        return int(end.group(2))
    steps = STEP.findall(stderr)
    return int(steps[-1][0]) if steps else 0


def seat_args(seat_json, rom):
    """The port role's boot arguments from a seat profile, with the release ROM and user folder replaced by {rom} and {user}."""
    path = pathlib.Path(seat_json)
    supervisor = json.loads(path.read_text())["supervisor"]
    role = supervisor["roles"]["port"]
    release = pathlib.Path(role["release_current"])
    if not release.is_absolute():
        release = path.parent / release
    release = release.resolve(strict=True)
    mapping = {"release": str(release), "runtime_root": supervisor["runtime_root"],
               "run_dir": str(path.parent / "parity-run")}
    command = [arg.format_map(mapping) for arg in role["command"]][1:]

    out = list(command)
    for flag, value in (("--rom", "{rom}"), ("--user-dir", "{user}")):
        out[out.index(flag) + 1] = value
    return out



def verdict(headless, tile):
    return {"ok": tile >= headless, "headless": headless, "tile": tile}


def run(binary, args, rom, seconds, picture):
    argv = [a.format(rom=rom, picture=picture, user=pathlib.Path(picture).parent / "user") for a in args]
    env = {**os.environ, "SDL_VIDEODRIVER": "offscreen", "SDL_AUDIODRIVER": "dummy", "N64C_RUN_ON": "1",
           "N64C_RUN_ON_LIMIT": str(max(seconds - 15, 10))}
    r = subprocess.run(["timeout", "-k", "10", str(seconds), str(binary), *argv], env=env, capture_output=True, text=True)
    return census(r.stderr), r.returncode


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--binary", required=True, type=pathlib.Path)
    p.add_argument("--rom", required=True, type=pathlib.Path)
    p.add_argument("--seat", type=pathlib.Path)
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--output", type=pathlib.Path)
    a = p.parse_args(argv)
    if a.seat and seat_args(a.seat, a.rom) != TILE_ARGS:
        print(f"tile_parity: the seat port command {seat_args(a.seat, a.rom)} differs from TILE_ARGS {TILE_ARGS}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as work:
        picture = pathlib.Path(work) / "boot.ppm"
        h, h_rc = run(a.binary, HEADLESS_ARGS, a.rom, a.seconds, picture)
        t, t_rc = run(a.binary, TILE_ARGS, a.rom, a.seconds, picture)
    result = {**verdict(h, t), "headless_rc": h_rc, "tile_rc": t_rc, "tile_args": TILE_ARGS}
    if a.output:
        a.output.write_text(json.dumps(result, indent=1) + "\n")
    print(f"tile_parity: headless {h}, tile {t} (rc {t_rc}): {'ok' if result['ok'] else 'TILE BOOTS SHORT'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
