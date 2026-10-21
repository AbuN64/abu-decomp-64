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
