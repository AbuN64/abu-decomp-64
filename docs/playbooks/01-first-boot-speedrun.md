# 01 First-boot speedrun

Goal: the native port boots the original game to its first real frame (a
menu, a title, a controller-pak prompt) built from the decomp's C.

## Why the obvious approach fails

Crawling the boot one crash at a time does not converge. Dozens of port lanes
that each fixed "the next stop" never drew an original frame: a crash is a late
and ambiguous signal, the bad value is usually written several functions before
the fault, and undefined behaviour makes the same stop move between trees.

## The approach that worked

1. **Link the decomp, don't rewrite the game.** Build the decomp's matched C
   against a small native compatibility layer (the platform library stand-in),
   a renderer and a mixer, 32-bit first so pointers keep their original width
   and game data can sit at its original addresses. Only the layer is new code.
2. **Trace the boot once in the oracle.** Run the original (the recompiled
   reference) from reset to the target frame and record every function's first
   hit, in order. That ordered set is the work list and the progress axis.
3. **Classify the whole list in one pass.** Unmatched boot functions are
   sorted at once: non-matching C (run `decomp behave` on all of them), asm
   only (write C that behaves first, match later), library asm (a layer row),
   unowned bytes (a split gap), oracle-only (read by hand). No onion peeling.
4. **Types and records first.** Rank shared structs and prototypes by how many
   boot functions each unblocks; land those before bodies.
5. **Make the host-ABI classes static.** Before booting, gates over every unit
   find the classes that only appear on the host: missing returns, dropped
   arguments, conflicting externs, sub-word aliasing, `long long`, struct
   alignment. Each gate emits one row per site with its class.
6. **Compare, don't crash (lockstep).** Every port boot reports the first
   function after which its memory, call arguments or returns differ from the
   oracle's at the same point. Classify a failure by its first divergence,
   never by where it crashed.
7. **One boot lists everything.** Link every compiling unit so the linker pulls
   what it needs; an unresolved name or a fault becomes a recorded stop and the
   boot runs on (watchdog on hangs, a sanitizer flavour for UB). One boot then
   lists every independent failure in boot order; later rows behind the first
   divergence are "probably downstream" until the next run.
8. **Head on main, hourly.** One script builds the port against the decomp's
   main tip, runs the run-on and lockstep boots, and publishes three numbers:
   matched-through x/N (headline), run-on reach x/N, and open failures by
   class. Fixes reach main within the hour or they are not progress.
9. **Triage routes, class lanes fix.** One seat reads the head numbers and
   routes each new failure to its class lane within 15 minutes. Fix lanes take
   rows by class, in parallel, in disjoint folders, with byte-identical
   original builds as their proof.

## Stop doing

- Reporting stop order or a screenshot as progress; use the head numbers.
- Fixing in a private tree for hours; land through main.
- Deferring a class "until the boot reaches it"; run its detector everywhere.
- Hang archaeology first; gates, then lockstep, then a hang core.
- Repinning per fix; pins move from the head tree.
- Putting first-light seats on byte matching; matching does not gate the boot.

## After the first frame, expect in this order

Host-only undefined behaviour, threads and timing, the mixer, data widths,
then branches the boot never took. Those show up while playing, which is
[02-tas-behaviour-loop](02-tas-behaviour-loop.md).

## Rules
- A boot-path function MUST land as one C body that behaves correctly (NON_MATCHING in its real file until it matches) serving both the port and the decomp, with no port-only fork or second copy.
