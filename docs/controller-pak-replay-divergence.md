# Port ran game code ahead of the oracle: the port never had the movie's Controller Pak

Card PjI_m2exrh37. Cause class: early state divergence (the Controller Pak slot), not frame pacing, VI/retrace count or
input-poll timing.

## Root cause

`tas._argv` named the run's card first (`--cpak <run>/card.mpk`) and then appended the profile's port arguments. The
rig profile (`tas.args`) carries its own `--cpak off`, and the port keeps the last `--cpak` it reads, so every replay ran
with an EMPTY Controller Pak slot while the oracle replayed the same movie with the card the movie was recorded with
(`cpak-start.mpk`). With a Pak the game takes another path at boot (Pak init and file scans stall the first frames), and
`Render_UpdateFrameTiming` (src/RENDER/80291054_5.CPP), which takes its retrace count per frame from `averageFour()` of
the last four measured frame lengths, ratchets to 4 VI per frame in the arenas. Without the Pak the same code stays at
its 2 VI fixed point. The port therefore produced twice the frames per poll, and every per-frame function showed a
port entry count far ahead of the oracle (81..11,531 polls).

## Trace (one function, one movie)

Movie `20261026T210033Z-monkey-wl7-arena-bunker-17-p1-2400`, port build c3d9c50 (rig live port), first 1000 polls,
`SetN64_UpdateFrameTiming` entries against the oracle snapshot `trace-poll-1000.tsv`:

| port argv                      | entries | retraces at poll 1000 | oracle entries |
|--------------------------------|--------:|----------------------:|---------------:|
| profile `--cpak off` last (old) | 994     | 1980 (2 VI/frame)     | 496            |
| run card last (fixed)           | 515     | 1021                  | 496            |

Oracle frames per 1000 polls follow the Pak exactly: Pak fresh (earth-temple, bunker-17, pak-create-2-8) 496 = 4 VI/frame;
no Pak (k1, options-menu, wl3) 976..978 = 2 VI/frame.

## Ruled out

- Frame pacing / retrace count: the shim retraces on its own virtual clock in both argv variants; 2 and 4 VI/frame are
  both fixed points of `averageFour()`, and the port reaches the oracle's regime (1021 retraces, 515 frames at poll 1000)
  as soon as it has the Pak, with no pacing change.
- CPU-cost model (virtual time per function entry): oracle entries per frame are 3006 (options-menu, 2 VI/frame) and 1795
  (arena-earth-temple, 4 VI/frame); a cost that gave temple 4 VI gave k1 and wl3 4 VI as well (504 vs 978/976). It was
  withdrawn (shim 589cb44).
- Input-poll timing: the replay feeds the same `movie.raw` lines in both hosts; the polls-to-retrace ratio changed
  (1000 polls: 1980 -> 1021 retraces) only with the Pak, i.e. the game polls differently, the runner does not.

## Fix

`tas._slot_free` drops any `--cpak` from the profile arguments, so the slot is always the run's own (card or `off`).
Test: `test_a_profile_cpak_argument_never_replaces_the_runs_card`.

## Left over

Port 515 vs oracle 496 entries at poll 1000: the shim charges no virtual time for Controller Pak bus transfers (the
oracle's SI stalls the first frames), a smaller gap than the one removed.
