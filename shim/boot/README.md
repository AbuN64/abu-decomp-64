# Boot tooling
Links a port's original C against the shim and walks the boot from stop to stop. The tools are driven by the port's
facts (`N64C_PORT_DIR`, see ../README.md) and by `--source` (the fetched original source tree); no game name lives here.

- `prepare.py` preprocesses one original source unit for the host: fatal stops for `INCLUDE_ASM` functions
  (`boot-missing: SYMBOL`, exit 78), unit inventory, known NON_MATCHING fallbacks. `unitstep.py` wraps a unit's steps in a cache.
- `policy.py`, `image.py`, `link.py`, `linkall.py`, `closure.py`, `addrscan.py`, `needs.py`: select the closure from the
  manifest, bind original names to shim functions (`shim-bindings.txt`), lay out the data image, generate bindings and linker scripts.
- `advance.py`, `sweep.py`: boot the run-on diagnostic build, classify each stop, route what cannot link.
- `trace.py`/`trace.c`, `lockstep.py`: boot-hit census and first-divergence compare against an oracle capture.
- `replay.c`: `N64C_REPLAY=PLAN` runs recorded function calls on the loaded image, each in a forked child (`decomp behave --replay`; format at the top of the file).
- `gates.py`: host-ABI gates over the original source (baseline `gates-baseline.tsv`). `fl0.py`, `tile_parity.py`, `hangcore.py`: frame and hang checks.
- C side: `game_main.c` (reset entry and shutdown), `frame.c` (frame capture), `runon.c`, `missing.c`, `trace.c`, `stop.h`, `boot.h`, `native.h`.
- Tests: `test_*.py` (`python3 -I test_X.py --compiler gcc --m32 --work DIR`). Those that read a port's real facts
  (`test_link.py`, `test_closure.py`) need `N64C_PORT_DIR`; the others run on `fixtures/` alone.

## Coverage flavour
A port built with `-DRAGE_DECOMP_COVERAGE=ON` compiles the original units with GCC `--coverage` and links libgcov;
`coverage.c` then writes the gcov counters on every way the process ends (the linker wraps `_Exit`, `_exit` and
`abort`; fatal signals are armed; `n64c_coverage_dump` is called before the boot stop, where a debugger holds and
kills the process). `GCOV_PREFIX` sends the counters to a per-run folder; the tas tooling turns them into gcovr reports.
