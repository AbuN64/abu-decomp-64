# Phase 5: native port (native memory, 64-bit, unlocked frame rate)

## Goal
Run the game as a clean native program: no emulated console memory, native pointer width (64-bit), and a frame rate
decoupled from the game's tick rate. These are build toggles. The decompilation is not changed for this phase. The
32-bit build on the console-compatibility layer (the shim) stays the default and is the reference every toggle is
measured against.

## Where console knowledge lives
One adapter boundary, the shim, holds everything that knows the console: memory map and address windows, MMIO and the
video, audio and peripheral interfaces, the console's clocks, and the byte layout of data read from the cartridge. The
game C and the port engine see native types and native memory only. The port itself does no console memory tricks:
no fixed-address mappings, no fault-driven memory emulation, no reserved address ranges.

## When it starts
All of these, measured by `decomp phase` on the reference version:

| Gate | Why |
|---|---|
| Phase 4 stage R4 (phase 3 done: every reachable function executed by play and behaving like the original) | A toggle is proven by replaying the whole movie set on both builds; code no movie reaches cannot be proven |
| No crash or hang in the frozen movie set on the reference build | A toggle's failures must be its own, not inherited |
| Pointer-bearing structures typed in the C (fields declared as pointers, not raw offsets or integers) | The 64-bit build widens what the C declares as pointers; an untyped field cannot be found |

Exact match does not need to reach 100% first. Matching bytes proves the C equals the cartridge; it does not prove the C
is safe to widen or to run faster. Coverage and the reference replay prove that. Work on the toggles may begin earlier as
measurement only: a readiness census (pointer fields typed, frame-rate-dependent code found) is shown on the phase page
from phase 3 on.

## Order of attack
1. **Native memory toggle (then 64-bit).** Game memory comes from ordinary native allocation, not a console address map.
   Cartridge data that holds pointers or console layouts is converted to native layouts once, when the installer
   builds the base pack, and the loader fixes up offsets to native pointers at load: the usual asset-pipeline pattern,
   so pointer width is never a property of the game C. Code that still does console address arithmetic (casts between
   integers and pointers, hard-coded segment addresses) is found by the readiness census and by replay, and fixed in
   the shim's adapter, not in the game C. With native memory working, 64-bit is a compiler target, not a code change.
   Proof: every frozen movie replays with the same frames and the same game state at every compared poll as the 32-bit
   reference (state compared field by field through the type info, since addresses differ).
2. **Tick rate vs frame rate toggle.** The game logic runs on a fixed tick (the original game's rate, the simulation
   clock); rendering runs at the display's frame rate and interpolates between the last two ticks (positions, camera,
   animation). Input is sampled per tick, so movies and the reference replay stay valid. Code whose behaviour depends
   on frame count rather than ticks is found by replaying at two frame rates and comparing tick state, not by reading.
3. **Both together**, then per platform: the release gate from the performance guardrail (frame time at the target
   rate and resolution on every target in the list, including the minimum-spec machine).

## Rules
- Toggles, not forks. The default build is unchanged; a toggle never edits the decompiled C, and a fix found through a
  toggle that is a real game bug goes back to the decompilation as clean C like any other.
- The 32-bit original-rate build is the oracle for both toggles, on every platform, for as long as the port exists.
- A toggle ships to players only after its own full-corpus replay is clean on every platform in the target list.
