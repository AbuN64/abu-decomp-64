# Phase 1: map every line, then the foundations

## Goal
Every byte of the cartridge is assigned to a function, a data row, a blob (graphics, audio, models, world, text) or
fill, and every function has C, matching or not. On top of that, the engine **foundations** are finished first as real,
matching, cleanly named C: random numbers, timing and frame pacing, controller and storage input, audio, and the display
list (graphics) layer. They are the parts a native port touches first and the parts every other system calls.

The foundation set is a floor, not a cap: the real C required before this phase is done is at least the larger of the
category minimums (every function of the set below) and a share of the function bytes the project's config names (the
first cut of a project sits around a tenth). A version already far past that floor does not wait on it.

## Done when (per version, reference version first)
- The progress block shows the ROM fully mapped (every byte has a source row).
- No function is still assembly only: every one has C, at least under `NON_MATCHING`.
- Every foundation function matches (no `NON_MATCHING`, no `FAKEMATCH`) and its body names no placeholder
  (`func_`, `D_`, `unk` names). `decomp phase` reports these gates; the project's config lists the foundation files.

## Order of attack
1. Mapping: data and blob rows before functions. They are mechanical (sizes and formats are in the bytes) and every
   later step needs the boundaries.
2. Drafts: a first C body for every function, compiled with `-DNON_MATCHING` in every version that holds it. A draft
   never changes the ROM, so its only proof is that the unit compiles. Land drafts in batches per file.
3. Foundations, smallest category first: random numbers, timing, input, audio, graphics. Inside a category, the
   functions the port reaches first come first.
4. For each foundation function: match it (hand C, build, compare the object with `objdump`), then name it and
   everything it owns, then move it to the file the evidence names.

## Membership of the foundation set
A function is a foundation when it is game code (not the SDK, which the port replaces) and one of these holds: it lives
in an audio, graphics, RSP or scheduler unit; its name says it reads controllers, plays sound, builds display lists or
paces frames; or it is a small direct caller of the SDK's controller, audio, video, RSP, timer or scheduler interface.
Larger callers of those interfaces are game logic that uses them. Per-effect sound triggers are game content. The
random number generator and its seeders are listed in the project's profile. A project may refine the first cut but
never past its own cap.

## Rules
- A header or type change is never part of a draft batch: it can change bytes, so it goes through the full check.
- Raw offsets and casts are allowed inside `NON_MATCHING` only; matched code uses typed fields.
- A function given up on still lands its best body under `NON_MATCHING` with a comment of how close it got.
