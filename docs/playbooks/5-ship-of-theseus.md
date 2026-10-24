# Phase 5: ship of Theseus (the port becomes its own code)

## Goal
Turn the port, plank by plank, from "the decompiled game running on a console-compatibility layer" into fully our own
modern code: no console hacks, native memory, 64-bit, tick rate separate from frame rate, clean engine boundaries. Every
plank is replaced only after the port already behaves like the cartridge, and every replacement is proven against that
behaviour.

The decompilation never changes for this. It stays a 1:1 rebuild of the cartridge, forever. Only the port diverges.

## When it starts
At phase 4 stage R4: phase 3 is done (every reachable function executed by play and behaving like the original) and the
frozen movie set has no crash or hang. Not at 100% exact match, and not earlier:

- Before R4 the port must consume the decompiled C unchanged. A behaviour difference then always points at the shim or
  at the decompilation, never at our own rewrites, which is what lets phases 2 to 4 converge.
- At R4 the port's behaviour is pinned by a full movie set. That set is what makes it safe to start replacing code: a
  plank that changes behaviour shows up as a failed replay instead of a bug report months later.
- Exact match is the decompilation's measure of byte equality. It says nothing about whether the port behaves, so it
  does not gate this phase.

During phases 3 and 4 the phase page shows a readiness census as measurement only: console-coupling sites by class
(fixed addresses, integer-pointer casts, segment addresses, overlay addresses, direct cartridge reads, frame-count
timing), so the first planks are chosen from numbers.

## The reference build
The R4 build is frozen as the reference: the decompiled C on the console-compatibility layer, 32-bit, original frame
rate. It stays buildable for as long as the port exists and is the oracle for every plank. Comparison is by game state
per tick through the type information, not by memory addresses, so planks that change memory layout can still be
compared.

## Planks
A plank is one unit of the port replaced by port-owned code: a translation unit, a subsystem, or one class of console
coupling removed across the code. The port keeps a ledger of every unit: still decompilation-sourced, or port-owned
(with the commit that took it over). The phase page shows the share of game code that is ours and the hacks left by class.

Default order (a project's config may reorder it):
1. **Address hacks out.** Game memory from native allocation instead of the console address map. Cartridge data that
   holds pointers or console layouts is converted to native layouts when the installer builds the base pack, and the
   loader fixes offsets up to native pointers. Code that does console address arithmetic is replaced plank by plank.
   No fixed-address mappings, no fault-driven memory, no reserved address ranges remain.
2. **64-bit.** With native memory, a compiler target rather than a code change.
3. **Tick rate vs frame rate.** Game logic on a fixed tick at the original rate; rendering at the display's frame rate,
   blending between the last two ticks; input sampled per tick so movies stay valid. Reuse the established
   matrix-recording interpolation pattern (record each transform per tick with a stable id, blend at render time)
   rather than inventing one.
4. **Console API adapters thinned.** Display-list, audio and peripheral calls move from translated console calls to the
   engine's own interfaces, one subsystem at a time.
5. **Modernise.** Engine boundaries (packs, settings, input commands, game module), tooling and code style, as the
   project's own design, with the ledger and replays as the safety net.

## Rules
- The decompilation is never edited for the port. A defect found while replacing a plank that is a wrong decompiled
  function (it does not do what the cartridge does) is fixed in the decompilation as matching C, like any other.
- One plank per change, proven before the next: full replay of the frozen movie set against the reference, by tick
  state, on every platform in the target list, plus the performance gate.
- No toggles that fork the port forever: a plank may sit behind a build option while it is proven, then becomes the
  only path and the old one is deleted. The reference build is the one permanent second path.
- Releases keep their stage rules: no player-facing change from a plank ships until its replay is clean everywhere.
