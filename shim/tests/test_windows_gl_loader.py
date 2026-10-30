#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Windows Fast3D archive must call GL 3 through its existing GLEW loader."""

import subprocess
import sys


def main():
    nm, archive = sys.argv[1:]
    symbols = subprocess.run(
        [nm, "--undefined-only", archive], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    framebuffer = [line.split()[-1] for line in symbols if "BindFramebuffer" in line]
    if not framebuffer or any("__glewBindFramebuffer" not in name for name in framebuffer):
        raise SystemExit(f"expected GLEW framebuffer reference, got {framebuffer!r}")
    print("Windows Fast3D framebuffer calls use GLEW:", ", ".join(framebuffer))


if __name__ == "__main__":
    main()
