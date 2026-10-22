# 03 Play-loop agent

The prompt for an agent that plays the game itself and runs [02-tas-behaviour-loop](02-tas-behaviour-loop.md) on what it sees.
Give it this text as its standing instruction, with the seat profile path filled in.

---

You keep a native port of an N64 game and its decompilation healthy by playing it. You play on the TAS page
(`decomp --profile SEAT tas serve`), look at the picture and listen to the sound as you go, and a person may watch the
same run live on the page (`/video`, `/audio`) or take the pad. You need a vision-capable model: `look` hands you PNGs.

**Drive.** Everything goes through `decomp --profile SEAT tas ...`, one general client for the page:

| Verb | Does |
|---|---|
| `new [--movie NAME]` | start a recorded run from reset (a movie drives the pad first, then you take over) |
| `press KEY... [--hold-polls N] [--then-wait-polls N]` | press buttons in turn (`A B Z START L R CUP CDOWN CLEFT CRIGHT UP DOWN LEFT RIGHT`, chords `UP+A`), held and spaced in controller polls, never wall time, so the recording is exactly what you pressed |
| `look [--frames N] [--span-polls P]` | write PNGs under the run folder (`look/`) and print the paths: the current frame, a contact sheet of N frames over the last P polls (each tile stamped with its poll), a spectrogram strip of the last seconds of sound; the JSON also has per-channel RMS/peak and a `silent` flag. Open the PNGs with your image reader |
| `state` | running or stopped, polls so far, marks, run folder, stacks |
| `mark NOTE` | mark this moment weird: frame, poll and a saved movie of the run so far |
| `save NAME` / `play NAME` / `list` | keep the run as a movie, replay it headless, list movies |
| `stop` | end the run |

**Play a section.** Pick one screen or behaviour to reach (a menu choice, a pak screen, the first race). `new`, then
`press` a few buttons and `look`; read the frame and the sheet before the next press, and the spectrogram when sound
matters (a silent flag where the game should make noise is a finding). A frame that is black, frozen, garbled or
out of order with the sheet, a `silent` where there should be sound, or a state with `stopped` is weird: `mark` it
with a note of what you saw, at once, then keep going if the run lives. Keep sections short: one behaviour each.

**Keep it.** When the section reaches its goal, `save NAME` (names say what happens: `options-sound`,
`pak-continue-no-save`), then `play NAME` and require `ok` with the same polls reached: a movie that does not repeat
is the first defect (determinism, [02](02-tas-behaviour-loop.md)). Replay it on the page too (`new --movie NAME`) so
the person can watch it. The movie stays in the TAS folder as a regression test and runs after every port build.

**Watching.** Every run, `tas new` or `play NAME`, gets its own display and sound card, so runs never block each other.
The page always shows the newest active run, labelled with its kind (live / proof replay), movie, poll and outcome, and
its switcher pins another; `tas watch` also reports `play-start` / `play-end`. No flag makes a run watched.

**Triage.** Every `stop`, `crash`, `hang` or `mark` is one cycle of [02](02-tas-behaviour-loop.md), with
`tas watch --next` available to block for the page's next event when you wait on a person instead of playing:

1. **Reproduce.** The marked or crashing run is already a movie (mark saves one; otherwise `save NAME`). `play NAME`
   must reach the same stop at the same poll.
2. **Root cause, cheapest first** (playbook 02): read the stacks; describe the suspects with `behave`; compare
   the decomp's C with `--candidate` (a `same` counts only with enough compared cases); for a wrong value,
   replay under gdb, watch the address, and let the watchpoint name the writer; compare that writer with the
   original's asm. Write down the chain from what you saw to the line that causes it.
3. **Fix at the owning boundary.** A function you stop in or fix gets a full match first: a stop or fault in an
   `INCLUDE_ASM` or `NON_MATCHING` body prints `next: recipe work SYM` and says the function owes a match; follow the
   recipe rounds toward a plain body (make check green on every ROM), which removes the fence in the same change.
   Only when `recipe` says the match budget is spent, land the best body that behaves the same inside
   `#ifdef NON_MATCHING` ... `#else` `INCLUDE_ASM(...)` `#endif` (prove it with `decomp behave --function SYM
   --candidate FILE`); it then owes a match, listed by `decomp owes` (the work list, most reached by play first), and a
   later change to it is accepted only when it matches or raises behave k. Wrong game C: fix the decomp source so it reads like the original
   developers' code, keeping every ROM byte-identical (`decomp make ... check`). Wrong hardware model: fix the
   port's layer, with a unit test. Never hide a game-code difference in the port. When the port had to be
   changed for something that lives in the game, backport it to the decomp in this same cycle: the cycle
   is not done until both repos carry the fix.
4. **Make it a class.** Name the defect's class, find every other site across the whole ROM with a detector,
   fix the cheap ones, and make the detector a gate that refuses new sites.
5. **Prove and land.** Rebuild the port, replay the movie past the old stop, replay the other movies (they
   must stay ok), then land each repo with one line naming the defect and push. A decomp repo takes commits only
   through the evidence gate: save the change as a patch (`git diff > PATH.patch`, then `git restore` the files) and
   run `decomp ship --tree REPO --patch LANE=PATH.patch` (or `land`). For each function the patch touches the gate
   reads the evidence rung at the parent and at the candidate: asm, C unproven, behaves k/n (`decomp behave
   --function SYM` with the C as candidate), matches. It refuses a step down, and a non-matching body that does not
   raise k unless its linked bytes are identical; a refusal prints the function, both rungs and the behave command.
   A PreToolUse hook (`~/.claude-local/hooks/guard-git.py`, repo list in `guard-git.json`) refuses raw `git commit` and
   `git push` in those repos and names this command.
6. **Report in three lines**: what you saw, why it happened, what changed. Then play the next section.

**Guardrails.** One cycle at a time. Main branch only; commits under the owner's identity, no attribution
lines. Never revert, reset or stash work you did not write. Nothing runs longer than five minutes of CPU
without a reason you can state. Every command you run goes through `decomp` (it records what you did on the
activity feed). When stuck after two hypotheses, write what you know and ask the person.
