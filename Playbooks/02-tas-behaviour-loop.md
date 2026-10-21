# 02 TAS behaviour loop

Goal: close the last functions and behaviours once the port draws the game.
After first light the remaining failures appear only while playing: a menu
choice that crashes, a screen that draws wrong, a path the boot never took.
This loop turns each one into named, behaving C in the decomp.

## The loop

1. **Play it once, record it.** Drive the port from the stream or a pad with
   recording on. Every controller poll is logged (buttons and stick), so a run
   is a replay from reset, not a video. Save the run under a short name that
   says what it does (`pak-continue-no-save`, `options-sound`).
2. **Replay it against the port only.** `decomp tas play NAME` boots the port
   with the movie as its only input and reports one outcome: `ok`, `crash`
   (with the fault subject and the guest backtrace), `hang` (no poll for the
   watchdog window), or `stop` (the port's own boot stop). Replays are
   deterministic because input is keyed to the poll index, not wall time. The
   oracle is not involved here; the port runs at full speed.
3. **Crash or weird: take the functions.** From the outcome take the guest
   frames of the faulting thread, plus the function that wrote the bad value
   when a watch names it. Those, and their direct callees on the path, are the
   suspects. A crash in a library or layer function means its caller passed a
   bad value; walk up until the frame is game code.
4. **Permute behaviour against the clean reference.** For each suspect run
   `decomp behave --function SYM` (no candidate first: it describes what the
   original reads, writes and calls), then with the decomp's current C as the
   candidate. `behave` runs the original recompiled from the ROM and the
   candidate side by side over seeded and captured inputs through Cake Taste's
   clean probe interface and names the first difference with its inputs.
   `differs` is the bug; `untestable` says what to capture next.
5. **Iterate the C until it behaves.** Fix the body, rerun `behave` until
   `same`. Keep it ostensibly what the original authors wrote: no host hacks,
   no `#ifdef` for the port.
6. **Give it a clean name, then land it.** Name the function, its locals,
   fields and file through Recipe (`recipe name` / `structure`). A function
   leaves this loop named and documented, never as `func_XXXXXXXX`. Land it
   through `decomp land` / `decomp ship`, matched if the bytes match, else as
   the file's non-matching body, so the port and the decomp share one body.
7. **Replay again.** The same movie must now pass or fail later. A movie that
   passes becomes a regression test: it stays in the TAS folder and runs after
   every port build.

## Determinism comes first

A movie is only evidence if replaying it plays the same game. Prove it before trusting any replay:
replay one movie twice and compare the per-poll shots (`N64C_TAS_SHOT_EVERY`); they must match byte for byte.
When they do not, find the sources before anything else:
- the game's random-number seed: find its generator (a linear congruential step on a global word) and the
  function that writes the seed; a constant seed is fine, a seed from a clock or counter is not;
- every clock the game reads (Count, osGetTime, VI count): in the port these must be the virtual clock,
  never wall time;
- thread order: the port's scheduler must run one guest thread at a time in a fixed order.

## Finding the cause, in order of cost

1. The stop names its subject and the run folder holds stacks; every stop path (host fault, code pointer,
   TLB miss, MMIO) must leave them, or that is the first defect to fix.
2. `decomp behave` on the top suspects. A verdict counts only with its counts: `same` over mostly
   untestable cases is no evidence.
3. A wrong value: replay under gdb, record the address at the point it was right, set a hardware
   watchpoint, and let it name the writer. Then compare the writer's stores with the original asm.
4. Lockstep (port vs oracle on the same movie) when the oracle runs: the first function whose writes differ.

## Every fix is a class

A defect found once exists elsewhere. Before closing a loop, name its class, write the detector that finds
every site across the whole ROM, and make it a gate that refuses new sites (a baseline that only goes down).
Cycle 1 produced three: struct fields whose names state an offset the layout does not give
(`unbake audit` `source.offset-name`), declarations that disagree with definitions (`gates.py --lto` over
every unit), and code words held as raw numbers in data (`addrscan`/`image`, including blobs and entries
inside a function's prologue).

## Rules

- A movie is input only. It names the port build and ROM digest it was
  recorded on; replay refuses a different ROM.
- Never fix the port's layer to hide a game-code difference that `behave`
  shows. Fix the layer only when the original behaves the same and the
  difference is in the hardware model.
- One movie per behaviour. Short movies that reach one screen beat one long
  playthrough; a failing long movie is cut down to the shortest prefix that
  still fails before triage.
- Journal each loop: the movie, the outcome, the suspects, the `behave`
  verdicts, the fix and the names given. The journal is evidence, written as
  it happens.

## Commands

| Step | Command |
|---|---|
| Record, save, list, delete | stream TAS controls, or `decomp tas save|list|show|delete` |
| Replay | `decomp tas play NAME [--build DIR]` |
| Describe / compare | `decomp behave --project TREE --host FILE --function SYM [--candidate FILE.c]` |
| Name | `recipe name`, `recipe structure` |
| Land | `decomp land`, `decomp ship` |
