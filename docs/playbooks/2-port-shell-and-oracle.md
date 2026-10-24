# Phase 2: the port shell and the reference

## Goal
Two programs that read the same controller input and can be compared poll by poll:
- **The native port shell**: the project's C compiled for the host (`decomp port new` and the shared `shim/`), linked
  against a small compatibility layer (video, audio, input, files, timing). It starts as soon as the boot path has C;
  the rest of the game runs as it gets C.
- **The reference** (oracle): a static recompilation of the original cartridge, used only as a test instrument. It never
  ships and no code is taken from it.

## Done when
The same boot movie replays on both, the port draws the game's own first picture, and the reference reaches the same
point. A movie is controller input keyed to the game's own controller polls, so a replay is the same run at any speed.
`decomp phase` reports this per game as the port gate and the oracle gate.

## Order of attack
1. Boot path C first (the functions from reset to the first frame), drafts allowed.
2. Port shell build: a 32-bit host build first when the game's pointers and structures are 32-bit.
3. Determinism: every entropy source seeded at boot (the random generator starts from constants; clocks are virtual),
   so the same movie twice gives the same state at every poll.
4. The reference replays the movie; differences at the first poll where they appear become work items.

## Rules
- Fixes found in the port for a cause in the game code land in the decomp in the same cycle, in the shared body.
- Data read at the wrong width or byte order on a little-endian host is fixed in the C types (bytes stay identical),
  never by swapping in the port.
- Nothing derived from vendor SDK source ships; the compatibility layer is written from public interface facts.
- ROM bytes never enter a repository or a package.
