# Phase 5: native port (64-bit and unlocked frame rate)

## Goal
Run the port as a native 64-bit program and at a frame rate above the original game's, as two build toggles. The
decompilation is not changed for this phase: both toggles live in the port. The 32-bit, original-rate build stays the
default and is the reference every toggle is measured against.

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
1. **64-bit toggle.** The port keeps the game's memory in one region and turns every stored pointer into a 32-bit offset
   into it when building for a 64-bit host, through one accessor in the port's game headers. The C reads the same; only
   the port's type definitions change with the toggle. Proof: every frozen movie replays on the 64-bit build with the
   same frames and the same guest-visible state at every compared poll as the 32-bit reference.
2. **Frame-rate toggle.** Game logic keeps its original rate; the port renders extra frames by interpolating between
   the last two game states (positions, camera, animation), so movies and the reference replay stay valid. Code whose
   behaviour depends on frame count is found by replaying at both rates and comparing game state, not by reading.
3. **Both together**, then per platform: the release gate from the performance guardrail (frame time at the target
   rate and resolution on every target in the list, including the minimum-spec machine).

## Rules
- Toggles, not forks. The default build is unchanged; a toggle never edits the decompiled C, and a fix found through a
  toggle that is a real game bug goes back to the decompilation as clean C like any other.
- The 32-bit original-rate build is the oracle for both toggles, on every platform, for as long as the port exists.
- A toggle ships to players only after its own full-corpus replay is clean on every platform in the target list.
