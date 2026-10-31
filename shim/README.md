# n64 shim
The game-agnostic N64 compatibility layer for AbuN64 ports: hand-written OS, display-list, audio, controller and
ROM-DMA code (`n64c_*.c`), the Fast3D glue (`fast3d/`), the vendored RSP audio core (`audio_abi/`) and the boot
tooling that links an original-C game against it (`boot/`). GPL-3.0-or-later; every file keeps its SPDX header,
vendored code keeps its notices (`audio_abi/SOURCES.md`, `audio_abi/PROVENANCE.md`, `AUDIT.md`).
It holds no game facts: a port owns those in its own repository and hands them over as below.

- `cmake/`: `n64compat.cmake` (the `n64compat` target and its tests), `n64compat_audio.cmake`, `fast3d.cmake`,
  `n64lint.cmake`. Consumer contract: set `N64C_M32` for a 32-bit build, provide an `SDL3::SDL3-static` target for the
  window/input/audio backends; Fast3D sources come through `n64c_dep(name repo pin [FetchContent_Declare options])`
  (`cmake/deps.cmake`, shared sources under `N64C_DEPS_DIR`; a consumer may define its own)
  (`CMakeLists.txt` here is the standalone shim build that CI runs: `cmake -S shim -B build && ctest`).
- `boot/`: the boot tools (`prepare.py`, `link.py`, `closure.py`, `advance.py`, `lockstep.py`, `gates.py`, ...) and
  their C halves (`game_main.c`, `frame.c`, `runon.c`, `trace.c`, `missing.c`). The port's facts are files in one directory,
  named by `N64C_PORT_DIR`: `manifest.json` (sources, entry, resources), `shim-bindings.txt`, `boot-trace.tsv`,
  `lockstep-accept.tsv`, `gates-baseline.tsv`, `known-fallback-units.txt`, `sched_glue.c`. See `boot/README.md`.
- The port's ledger directory (`--ledger`, default `platform/port`): `ENTRY_POINTS.tsv` is the deprecation ledger (symbol,
  kind, subsystem, shim_component, native_replacement, removal_lane, status; rows are open or removed), `OPEN_COUNT` may
  only fall and `VERSION_DEBT` counts VERSION_ conditionals still allowed. `tools/n64lint.c` enforces it on every port
  build and exempts the shim and that directory, the only places N64 names may appear. Marking a row removed bans the
  name everywhere, so flip it only in the commit that deletes the last use. The ledger stays per port.
- `decomp port new` (abu-decomp-64) scaffolds a port on this shim.

The shim is hand-written C with no third-party code. AUDIT.md explains why the pinned libultraship is not linked.
- n64c_sched.c: guest threads (one runs at a time, by priority), mesg queues, timers, CPU/RCP interrupt masking, and cache no-ops.
- n64c_vi.c: retrace timing and buffer swap. n64compat_glue.c: frame driver (n64c_frame) and RSP task dispatch.
- n64c_pi.c: ROM DMA with a post-DMA endian fixup keyed by registered resource type. n64c_cont.c: controllers, EEPROM, Pak as files.
- n64c_dl.c: display-list boundary (RSP addresses to host pointers, L3DEX2 lines and screen-space vertex edits as F3DEX2) for the pinned Fast3D renderer (fast3d/, cmake/fast3d.cmake; AUDIT.md).
- tests/: unit goldens; the Fast3D replay gate is n64c_f3d_replay + tests/replay_ssim.py. Build: cmake -DRAGE_N64COMPAT_ONLY=ON [-DRAGE_N64COMPAT_M32=ON]
  [-DN64C_REPLAY_CAPTURE=<capture dirs (list) with capture.json>] ; captures hold game memory and stay out of the repository.

AI output: `osAiSetNextBuffer` forwards a word-swapped S16 stereo buffer through
`n64a_ai_submit`. Full builds use SDL3; shim-only builds use a null sink. Tests
can replace the same callback with `n64c_set_audio_sink`. Buffers are copied
synchronously into a two-entry FIFO on the virtual clock; `OS_EVENT_AI` is posted
when each buffer's playing time has elapsed (the game refills on it), and hardware
rate-divider quantization is not emulated. SDL errors return -1.
`RAGE_N64COMPAT_M32` selects 32-bit compilation for the whole project, including
SDL and the audio ABI, so every linked library has the same architecture.

Host seam: everything that is an operating-system call (address windows, fault handling, guest thread stacks,
the stderr log, process start) sits behind `os/n64c_host.h`; `os/os_posix.c` and `os/os_posix_proc.c` are the Linux
backend, `os/os_win32.c` and `os/os_win32_proc.c` the Windows one (mingw-w64, i686). The generic modules hold no OS
call. Windows maps views at 64 KiB granularity, so a guest TLB page the host cannot alias (4 KiB, or not 64 KiB
aligned) is kept coherent in software (see the head of `os/os_win32.c`); the Windows build also needs the process
start (`n64c_host_process_start`) to claim the guest's virtual range `0x00400000..0x01000000` before the loader's
stack and heap take it: the ranges the game uses are reserved as 64 KiB placeholders (`os/win_space.c`, tested against a
model of the Win32 rules in `tests/test_win_space.c`), a TLB mapping replaces the placeholders it covers and an unmap
restores them, and a mapping that fails prints what occupies the range. The game image at 0x10000000 (`--image-base`). The boot tools read the target's object
conventions from `boot/hostabi.py` (the leading underscore of PE symbols, the target's nm and ar).

Native address windows: `n64c_memory_open` supports Linux/i386 and Windows/i386 and refuses
other hosts. It maps shared RDRAM at KSEG0/KSEG1, a read-only typed cartridge
view through the existing PI loader, and protected PI/VI/AI/SI register pages.
It never maps over an existing allocation. Call it after registering resource
layouts; close only after guest threads stop (shutdown handles this order).
A register fault runs one native instruction and resumes the same register/FPU
context. Device calls occur on a separate per-thread stack outside signal
handlers, so scheduler handoffs and file/audio callbacks retain their normal
contracts. The single-running-guest-thread scheduler is required. Existing
process signal handlers are preserved by refusing installation over them.
Only aligned32-bit register operations are supported.

Current register coverage: synchronous PI ROM-to-RAM DMA/status, VI origin and
configuration, AI frequency/buffer submission, and SI query/controller-poll
packets through existing components. VI scanline reads, cartridge writes and
other raw PIF commands stop explicitly; this is not cycle-accurate hardware
or FL0 acceptance. Unknown memory faults retain the default signal behavior.
The cartridge view applies registered resource endian layouts; unknown mixed
structs/pointers still require typed loader/relocation facts before boot.

Typed PI layouts accept `p` for a serialized32-bit pointer. A single registered
resolver maps its original function/data identity to a native value; null remains
null. Missing bindings fail the read and send no completion event. The caller
must discard a failed destination. Registration rejects overlaps, empty/truncated
layouts and partial records. Reads of partial multibyte fields refuse instead of
silently producing a malformed native value. This is a loader mechanism; the
decomp owner must still supply the actual boot-resource types and bindings.

Interrupts use one active CPU/RCP mask in the cooperative native scheduler.
`osSetIntMask` returns the previous mask; event bits latch while masked and
deliver once when enabled. Ordinary messages still work while interrupts are
disabled. VI notifications and timer servicing use the same mask. The mask is
global in this native model; per-thread mask save/restore is not implemented.
`__osDisableInt`/`__osRestoreInt` preserve the global-enable bit for nested
critical sections. `n64c_running_thread` is a native extension returning the
calling guest's OSThread, or NULL on the host. It supplies the observed
running-thread getter without importing an original libultra body.

`osUnmapTLB(index)` removes both aliases owned by that entry. Remapping an
entry and unmapping all entries use the same table. These adapters were written
from the public API contracts, not an original implementation:
[interrupt masks](https://ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/os/osSetIntMask.htm)
and [TLB management](https://ultra64.ca/files/documentation/online-manuals/man-v5-1/pro-man/pro10/10-06.htm).

`osCreateViManager` uses the VI/timer service already owned by the native
host driver after `n64c_init`; repeated calls preserve its current state.
Its priority does not create a guest thread. `guMtxIdent` writes the documented
64-byte packed identity matrix in host word order. `n64c_probe_tlb` is the native
extension for a TLB-only lookup and returns UINT32_MAX for no mapping.
`osVirtualToPhysical` now resolves mapped TLB aliases using that same table,
while direct RDRAM and native host pointer handling keep their existing roles.
Contracts: [VI manager](https://ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/os/osCreateViManager.htm),
[identity matrix](https://ultra64.ca/files/documentation/online-manuals/man-v5-2/allman52/n64man/gu/guMtxIdent.htm).

`__osSetCompare` schedules one counter interrupt at the programmed32-bit Count
value in the native virtual clock. Unsigned distance handles wraparound; an
already-equal Count is serviced on the next clock advance. Reprogramming clears
the pending Compare event and replaces its deadline. It does not cancel native
software timers. The event uses the same CPU mask and deferred delivery as other
interrupts; no original timer-manager thread is imported. Contract:
[R4000 User's Manual, Compare register(11), p104](https://www.eecg.utoronto.ca/~moshovos/ACA/R4000.pdf#page=134).

The virtual clock moves when the host advances it (frames, idle), and a guest
thread holding the CPU keeps the host from running. So a thread that waits on
the clock in a loop (a delay, or a fault handler's watchdog before it resets)
would never see time pass: past 1024 `osGetTime`/`osGetCount` reads in one hold
of the CPU, each read advances Count by 48 (about a microsecond, one loop pass)
and timers that come due fire and preempt as on the console. Every TLB miss the
guest's fault service gets is logged as `n64c-fault: TLB-miss-<address> load|store
at host pc <pc>`, so a wild pointer the guest answers with its crash path names
its code in the boot log.

### Live controller input

The visible SDL3 window installs `n64c_input` at creation and samples keyboard and
SDL_Gamepad state after its event pump. SI reads latch the last complete snapshot;
they never call SDL from a guest thread. Channel zero remains present with or
without a physical pad because its keyboard fallback is always available.
Offscreen/dummy video and any `N64C_LOCKSTEP` environment disable the provider.
No SDL gamepad subsystem is opened in those modes; scripted providers stay usable.
Closing/resetting the shim unregisters the provider and releases its gamepad.

`n64c_input_config.c` holds the generic mapping table and raw axis thresholds.
Applications can pass an immutable `N64cInputConfig` to change bindings. Defaults:
arrows = stick; X/C/Z = A/B/Z; Enter = Start; A/S = L/R; I/K/J/L = C up/down/left/right.
Gamepad south/east = A/B; west/north = C left/up; right stick = all four C buttons;
D-pad, Start and shoulders map directly; either trigger = Z. Left stick uses an
8000-unit axial deadzone and integer rescaling to -80..80 (up is positive Y).
Keyboard and pad buttons combine; stick contributions add and clamp. Opposing
arrow keys cancel. Only the focused game window consumes keyboard state.
The first available gamepad owns channel zero; disconnect releases it and promotes
an already connected pad, while SDL add/remove events handle subsequent hotplug.
