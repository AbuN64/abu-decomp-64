# Port virtual clock: per-call verdicts before and after the SI/Pak latency + VI catch-up patch

Measured with the per-call verdict (behave/percall: port and original calls aligned by function, caller, ordinal),
which replaced the count-based verdict. Six Pak/menu movies, each replayed with the live port (before) and with the
same port plus the patch (after); the original side is one capture per movie (lockstep.jsonl of the before run), so
both verdicts are judged against the same original. Port: rage-ports 21cc576, shim 37d2d7d (+ patch), rw fba39c9.

## Result: the patch moves no verdict, so it is not adopted

Columns: behaves / differs (args+result) / missing / port-only.

run | before behaves/differs/missing/port-only | after | functions better/worse
controller-pak-menu | 359/315/277/2 | 359/315/277/2 | 0/0
monkey-wl1-start-pak-create-players-1-7-2400 | 350/296/968/2 | 350/296/968/2 | 0/0
monkey-wl1-start-pak-load-players-0-9-2400 | 358/316/463/2 | 358/316/463/2 | 0/0
monkey-wl1-start-title-screen-2-8-2400 | 344/306/773/2 | 345/306/772/2 | 1/0
monkey-wl3-multi-pak-create-players-10-p1-2400 | 350/296/437/2 | 350/296/437/2 | 0/0
monkey-wl1-start-pak-prompt-1-7-2400 | 337/308/959/2 | 337/308/959/2 | 0/0
total differs 1837 -> 1837

The differs-result of a run (259 on controller-pak-menu, the sampled one) are audio structure digests, not clock effects: the memory digest of
Audio_InitializeVoiceRecord, Audio_StopVoicesByFlags, Audio_UpdateDialogPlayer, Audio_UpdateMusicFade ... differs in
byte order of a halfword pair (original 0000ffff, port ffff0000) or in a field the original has already advanced
(Audio_UpdateMusicFade ordinal 2048: original 00001809, port 00000e55). The differs-args are tagged input (an upstream
call already differs). Neither class depends on the SI/VI clock.

## Where the original spends time (read from its scheduler, not assumed)

The original host runs with N64R_CLOCK=entries (cake-taste-64 hosts/n64recomp/cpu_scheduler.cpp): its clock advances
only per function entry, spin pass and interrupt restore. It charges no SI, Pak, RDP or RSP time. So the card brief
"charge SI/Pak transfer time and RDP/RSP-bound frame cost" has no oracle-measured basis: the only costs the oracle
has are CPU per entry (the model withdrawn in shim 589cb44). That matches the table: SI latency + VI catch-up leaves
every verdict unchanged, and the earlier RCP frame-cost runs (SD-FRAME-COST-NOTES.md) raised differs.

## Frame numbers are not comparable between the sides

At the same call ordinal the frame field differs by a factor near 2 (Audio_UpdateMusicFade ordinal 2048: original
frame 2057, port frame 1067; Audio_StopVoicesByFlags ordinal 0: original 34, port 1). Until the two sides count the
same frame unit, a per-poll phase check from calls.jsonl cannot tell clock drift from a unit mismatch.

## What blocks the acceptance items

- arena-bunker-17 polls 1000/11550: the port stops at about 1790 polls (n64c_boot_stopped, an undecompiled function);
  the original needs a lockstep capture for it. Here the pak movies stop at 1263-2273 polls.
- Only 8 arena runs plus these 6 have both sides captured; the rest need an oracle replay (about 10 h on the pool).
- No baseline number below 921 differs exists: that figure was the count verdict, which is gone.
