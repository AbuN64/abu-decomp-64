# n64compat license and provenance audit (FL-P1-GFX, step 0)

Scope: libultraship (LUS) pinned at `c6ecfdb491cc3a0d08aa04eff2f17f0bf5946297` (tag 2.0.0.0, 2026-08-15, 486 tracked files) and its Fast3D renderer (`src/fast`, `include/fast`). History was read from a full blobless clone of `Kenix3/libultraship` (1,239 commits); the pinned tree was read from `$STATE/shim-spike/src`. Rule applied: owner clean-source rule of 2026-10-18 17:22 (OSI licence and no derivation from decompiled or leaked Nintendo code or SDK material).

## Verdict

**Nothing from LUS or its Fast3D renderer is linked.** The graphics-relevant and OS-relevant parts fail the rule, so every one was replaced by a hand-written equivalent in `platform/n64compat/` (table below). The LUS pin is kept only as audit input; the build no longer fetches it, so `n64compat` has zero third-party code.

| Verdict | Files | Meaning |
|---|---|---|
| clean | 368 | LUS-own framework, build, docs, tests, rendering-API wrappers. MIT, no SDK/decomp material found. Not needed, so not linked. |
| grey | 62 | The libultra and GBI headers, the opcode tables, the os_* implementations and the bridge code that includes them. |
| unknown | 56 | Fast3D interpreter and support code, small LUS headers whose only dependency is a grey header, font data. |

Per-file verdict and evidence for all 486 files: `AUDIT-files.tsv` (columns path, verdict, evidence, linked). The decisive files are listed individually below.

## Licence

- `LICENSE`: MIT, Copyright (c) 2022 kenix3. Covers LUS's own contributions only. It says nothing about the origin of imported headers.
- `src/fast/LICENSE.txt` and `include/fast/LICENSE.txt`: MIT, Copyright (c) 2020 Emill, MaikelChan (the Fast3D renderer lineage).
- No file in the pin carries a Nintendo, SGI or sm64-decomp licence header or provenance statement. The absence is part of the problem: the SDK-derived headers had their original notices stripped, so their MIT label is not evidence of clean origin.
- Third-party fetched by LUS's CMake (not linked, listed for completeness): ImGui (MIT), StormLib (MIT), libgfxd (glankk, MIT), thread-pool (MIT), Monocypher (BSD-2/CC0), SDL3 (zlib), spdlog and nlohmann_json (MIT), libzip (BSD-3), tinyxml2 (zlib), prism-processor (KiritoDv; licence not verified), TinyCC (LGPL, off).

## Evidence for the grey and unknown verdicts

| File(s) | Verdict | Evidence |
|---|---|---|
| `include/libultraship/libultra/*.h` (25 files: gbi, gs2dex, mbi, rcp, rdp, abi, os, thread, message, pi, vi, pfs, controller, sptask, r4300, ...) | grey | First imported 2021-09-12 in `6610d4e` "Added fast3d (WIP modifications)" as `otrlib/Lib/Fast3D/U64/PR/ultra64/*`, renamed `PR_` in `38afeec` (2021-12-22), moved in `2aecab6` (2022-11-01). That `PR/ultra64` layout is the sm64-port include tree. `gbi.h:39` holds the sm64-port idiom "To enable Fast3DEX grucode support, define F3DEX_GBI.", `gbi.h:44` the `_DW(macro)` wrapper and a forced `F3DEX_GBI_2`. `thread.h`, `message.h`, `pi.h`, `vi.h`, `pfs.h`, `controller.h` carry decomp-style `/* 0x00 */` offset comments and `// size = 0x1B0` trailers, which come from reconstructing SDK structs. `abi.h` is the audio command macro set in SDK shape. No provenance is stated anywhere. |
| `include/fast/lus_gbi.h`, `f3dex.h`, `f3dex2.h` | grey | Added 2024-05-05 in `5b7164e` ("GFX Refactor Stage 2") as a retyped copy of the gbi.h opcode tables, same names and the same `/* -1 */` trailers (`lus_gbi.h:9-30`). |
| `src/libultraship/libultra/os_mesg.cpp`, `os_pi.cpp`, `os_vi.cpp`, `os_time.cpp`, `os_eeprom.cpp`, `os_cache.cpp`, `os_vm.cpp`, `os.cpp` | grey | Written 2024-05-06 (`6dc4bd1` "Libultra implementations"), compiled against the grey headers, shaped to the decomp layout (`OSMesgQueue` is 0x18 bytes). `os_mesg.cpp` ignores the block flag, so it cannot serve threads in any case. |
| `src/fast/interpreter.cpp`, `include/fast/interpreter.h` (5,368 lines) | unknown | Own MIT code in the Emill n64-fast3d-engine lineage. Grep finds no decomp or SDK text (no "sm64", "decomp" or "leak" hit). It includes `lus_gbi.h` and `libultraship/libultra/os.h` (lines 28 and 37), so it cannot be built without grey headers, and its microcode behaviour tables mirror the original. The renderer cannot be linked clean while the headers are grey. |
| `src/fast/resource/**`, `Fast3dGui`, `GfxDebugger` | unknown | Same lineage; they depend on the interpreter. |
| `include/ship/window/gui/Fonts.h`, `IconsFontAwesome4.h` | unknown | Embedded font data and icon table with licences of their own. |

Clean with evidence: `src/ship/**` and `include/ship/**` (resource manager, config, window, controller deck, audio sinks), `src/fast/backends/**` (OpenGL, D3D11, Metal wrappers), `src/ship/utils/StrHash64.*` (BSD notice from libcore Crc.cpp, modified). A grep for silicon graphics, nintendo, sgi, ultra64, copyright 19, decomp, sm64, leak and decompil over the pin returns only comments (button names, doc links to the public ultra64.ca manual) and the files listed above.

## What replaces each grey or unknown part

All hand-written C, from public documentation (n64brew wiki, the public GBI/F3DEX2/L3DEX2 command descriptions, the libultra function reference). No code from sm64-port, decomp, SoH, PD, LUS, GLideN64 or RT64 was copied. Files are in `platform/n64compat/`.

| Replaces | New file | Basis |
|---|---|---|
| libultra headers (types, mesg, thread, vi, pi, cont) | `n64c_os.h` | Public API names and argument lists; structure field lists as documented. |
| os_mesg, threads, timers | `n64c_sched.c` | Standard libultra semantics. |
| os_vi | `n64c_vi.c` | |
| os_pi, ROM DMA | `n64c_pi.c` | |
| os_cont, eeprom, pak | `n64c_cont.c` | |
| Display-list boundary | `n64c_dl.c` | RSP addressing, F3DEX2 command layouts and the public gSPLineW3D/gSPModifyVertex descriptions; G_LINE3D and G_MODIFYVTX XYSCREEN/RGBA become plain F3DEX2 commands for Fast3D (n64c_dl.h) |

Honesty note: while auditing, the author read LUS's `thread.h`, `message.h` and `os_mesg.cpp`. `n64c_os.h` uses the same documented field and constant names (`OSThread.next/priority/queue/tlnext/state/flags/id/fp/thprof/context`, `OSMesgQueue.mtqueue/fullqueue/validCount/first/msgCount/msg`, `OS_EVENT_*`). Those names are the libultra programming-manual API and cannot be avoided if game code is to link, but an independent reviewer should confirm that no layout comment or ordering was carried over. The algorithms in `n64c_sched.c` are original (baton passing, priority run queue).

Ledger consequence: no row's `shim_component` names LUS any more; they name the n64c adapters. The pin variables were dropped from `cmake/n64compat.cmake`.

## Re-audit of the Fast3D interpreter (PORT-OPUS-2, 2026-10-19)
Owner decision 2026-10-18 21:00: replace the grey headers with clean ones written from public
facts, then use Fast3D if its interpreter is clean.
- `src/fast/interpreter.cpp` (5,368 lines), `include/fast/interpreter.h` (576): verdict changes from
  unknown to **clean (own code)**. History at the pin: 245 commits; the file enters LUS in `3330b67`
  (2021-10-11, Kenix, "git subrepo clone https://github.com/Emill/n64-fast3d-engine"), i.e. Emill's
  MIT renderer, then LUS/HarbourMasters edits. A grep for SDK or decomp text (libultra, Nintendo, SGI,
  sm64, decomp, leak, os*/gu* implementations) finds only the `libultraship/libultra/os.h` include
  (line 37). It implements no libultra function; its microcode behaviour is reproduced behaviour, not
  copied microcode. One external source is named: lines 1573-1574 take MM point-light coefficients
  from GLideN64 `src/gSP.cpp#L636` (GPL emulator, a clean reverse-engineering source, not Nintendo
  material; its licence terms must be honoured where the port ships them).
- What it needs from the grey headers: 188 identifiers (`lus_gbi.h`, `f3dex.h`, `f3dex2.h`,
  `libultra/*.h`): command opcodes, combiner/blender mux values, othermode shifts, image formats,
  `G_MW_*` indices, and the in-memory shapes of Gfx, Vtx, Light, Vp, uObjBg, uObjSprite. These are
  interface facts available from public microcode documentation; clean replacements live at the
  port boundary (`platform/n64compat`) and are put first on Fast3D's include path. No LUS file is forked.
- Build footprint (planning fact): the interpreter also includes ship/ (Gui/ImGui, ResourceManager,
  ConsoleVariable), spdlog/fmt, fast/debug/GfxDebugger, fast/resource types and a rendering backend.
- The hand-written n64c_gfx/n64c_raster renderer is retired: Fast3D passed the replay/SSIM gate, and the original-C boot captures its frames through fast3d/n64c_f3d (one framebuffer per colour image).
- Clean headers (PORT-OPUS-3): `fast3d/include/fast/{f3dex2,f3dex,lus_gbi}.h` and
  `fast3d/include/libultraship/libultra/{types,os}.h`. Every value was checked equal to the pin by
  compiling both and comparing (271 names in `lus_gbi.h`, sizes of Vtx/Vp/Light/uObjBg/uObjSprite/Gfx
  at -m32). The LUS extension opcodes (`OTR_G_*`, `*_WIDE`, `*_INTERP`) and the interpreter-internal
  combiner selectors (`G_CCMUX_KEY_*`, `G_CCMUX_CONVERT_*`) are LUS's own MIT interface.
- Build (`cmake/fast3d.cmake`, `RAGE_FAST3D`): `src/fast/**` and `src/ship/**` from the pin, minus the
  grey `src/libultraship/**`, the OTR display-list/vertex/matrix resources and factories (they need
  the game-facing GBI), scripting/MPQ (off) and non-Linux backends. Each grey LUS include path without
  a clean replacement gets an `#error` stub ahead of the LUS include directory (generated from
  `AUDIT-files.tsv`), so a grey header can never be compiled in. Dependencies at LUS's pins: Dear
  ImGui (MIT, with LUS's MIT config patch), thread-pool (MIT), prism-processor (MIT), Monocypher
  (CC0/BSD-2), stb_image (public domain/MIT); system spdlog/fmt/tinyxml2/libzip/nlohmann-json (MIT/BSD).
- Interface gap found (not a provenance issue): `Interpreter::SegAddr` treats an operand as segmented
  only when bit 0 is set (LUS's own convention) and otherwise as a host pointer. Original display
  lists use RSP addressing (segment in bits 24-27, physical or KSEG addresses). The port therefore
  hands Fast3D copies made by its own display-list boundary, `n64c_dl.c`, written from the public
  F3DEX2 command layout.
