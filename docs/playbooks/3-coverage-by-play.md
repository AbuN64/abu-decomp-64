# Phase 3: one version to full coverage by play

## Goal
For the reference version, every function the game can reach is executed by a recorded movie on the port and behaves
like the original. Movies are played the way a speedrunner routes a game: short, fast, many of them, each aimed at code
no other movie reaches. Bots replay and extend them at turbo speed.

## Measurements (the same numbers gate phase 4)
- **Function coverage**: functions with an execution count above zero, over the union of all movies.
- **Line and branch coverage**: the same union, per line and per branch (the port is built with coverage counters).
- **Byte reach**: bytes of the functions reached, over all function bytes. This is "how much of the ROM's code ran".
- **Reference reach**: the same, from the reference's function-entry trace. A function the reference reaches and the
  port never does is a port defect; one nobody reaches needs a new movie.
- **Behaviour**: each reached function compared with the original on the same inputs (`decomp behave`), and the game
  state compared region by region at every poll of a movie.
Measured by one coverage build, `decomp tas play --all`, and `decomp tas coverage`; `decomp phase --refresh` keeps the
report current.

## Done when
Reachable function coverage is 100% (functions proven unreachable are listed with the proof: no caller, debug only,
another version only), no function the reference reaches is missing on the port, and no reached function behaves
differently. Lines and branches the original never takes (defensive code) are listed by file and line.

## Order of attack
1. Run every movie; group the findings (crashes, hangs, state differences, sanitizer reports) by root cause, not by
   movie.
2. Fix one root cause at a time in the shared C, then write it as a rule and sweep the whole tree for its siblings.
3. Aim new movies at the largest unreached functions nearest to reached code: a start point (a saved game, a menu, a
   level) per area.
4. Replay everything after each landing; a movie that got worse blocks the landing.
5. Backport every port fix to the decomp in the same cycle; a cycle is done only when both land.
