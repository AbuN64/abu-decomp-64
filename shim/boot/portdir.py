# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the port keeps its facts: manifest.json, shim-bindings.txt, boot-trace.tsv, lockstep-accept.tsv,
gates-baseline.tsv, known-fallback-units.txt, sched_glue.c. They belong to the port, never to the shim;
N64C_PORT_DIR names the directory (without it the names resolve against the working directory)."""
import os
import pathlib


def fact(name):
    root = os.environ.get("N64C_PORT_DIR")
    return pathlib.Path(root, name) if root else pathlib.Path(name)
