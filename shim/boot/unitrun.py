# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point of a unit step: `python -I -S unitrun.py <unitstep.py options>`. An interpreter compiles the script it
is given on every start and caches only what it imports, so this stays three lines and unitstep.py (the whole step
runner) loads from its cached bytecode: a restored unit is then ~5000 starts per fresh build tree, not 5000 compiles."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unitstep  # noqa: E402

raise SystemExit(unitstep.unit(sys.argv[1:]))
