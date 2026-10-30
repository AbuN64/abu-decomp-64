# Boot tooling
Links a port's original C against the shim and walks the boot from stop to stop. The tools are driven by the port's
facts (`N64C_PORT_DIR`, see ../README.md) and by `--source` (the fetched original source tree); no game name lives here.

- `prepare.py` preprocesses one original source unit for the host: fatal stops for `INCLUDE_ASM` functions
  (`boot-missing: SYMBOL`, exit 78), unit inventory, known NON_MATCHING fallbacks. `unitstep.py` wraps a unit's steps in a cache (`unitrun.py` is its entry point: run it as `python -I -S unitrun.py`, so the step runner loads from bytecode and a restore answers from stat signatures without parsing the digest memo).
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

Original C units import OS, audio and matrix calls through `n64c_guest_os.h`,
`n64c_guest_al.h` and `n64c_guest_gu.h`. These are hand-written declarations
from the shim's implemented API and caller signatures, with no SDK header
content. They describe the original 32-bit calling boundary: a game keeps
its own records and passes pointers or one-word handles. Native shim units
continue to include `n64c_os.h`, `n64c_al.h` and `n64c_gu.h`; the two C type
models belong in separate translation units. Ports resolve guest names through
their explicit shim binding ledger. Unsupported calls still fail through the
existing boot policy, rather than importing SDK implementations.

The standalone shim build's `n64c_guest_imports` CTest calls the real native
message queues, matrix constructor and linked-list operations through these
guest declarations. It checks message identity, queue overflow/underflow,
record offsets, matrix contents and list links on the required 32-bit ABI.

`native.h` imports the compiler's `<stdarg.h>` without shadow guards or MIPS
argument macros. Native callers and formatters carry `va_list` throughout.
The `n64c_native_varargs` test covers promoted integers, doubles, 64-bit integers,
pointers, forwarding and `va_copy`. The same source can be cross-linked with
`<target-cc> -Wall -Wextra -Werror -shared -Wl,--no-undefined
shim/tests/test_native_varargs.c -o <output>`; host CTest also executes it.
