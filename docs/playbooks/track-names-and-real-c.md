# Parallel track: clean names, structure, and real C

Runs beside every phase, not after them. Two strands, one rule: the shipped C is what the original programmers
plausibly wrote.

## Real C, no cheating
- No inline assembly, `.word`, pragmas or section tricks in ROM-path C. A near miss stays under `NON_MATCHING` with its
  best body and a comment of how close it got.
- A match that only holds through a trick carries a `FAKEMATCH` comment and still counts as open work.
- Prove a match by the byte check (hand C, build, `objdump` against the original), never by a tool that searches for
  bytes. The single gate is `unbake cheat`.
- One body serves both goals: the same C builds the original bytes and compiles for the port; there is no port-only copy.

## Clean names and file structure
- Functions, globals, struct fields, locals and types get names from the evidence: debug symbols left in the
  cartridges, assertion strings, call context, and the other versions' names. Placeholders (`func_`, `D_`, `unk`) shrink
  over time; the count is a tracked number.
- Functions move to the file their evidence names, so the tree reads like the original source layout.
- Naming and moves go through the Recipe Maker (`recipe name`, structure passes) so each rename is reviewed, recorded
  and repeatable, and a rename never changes the ROM bytes.
- Names and moves follow the reference version first; the other versions inherit them through the shared body.

## Order of attack
1. Foundations first (phase 1 requires them clean), then the functions the port reaches first (phase 3 order).
2. Types before names: a struct field named once clears every function that touches it.
3. Land small and often; a rename pass that touches many files is checked as a whole before it lands.
4. Re-run the placeholder count after each landing and let the number steer the next pass.

## Full rename pass
When most placeholders must go quickly, names are written as rows and landed in batches. A lane never holds a tree,
a lease or a diff.

1. **Rows, not diffs.** A row names one target by a stable key, never by file or line, so it stays valid while other
   renames, file moves and matches land: `fn:VER:VRAM8` (a function), `datum:VER:VRAM8`, `member:TAG+OFFSET` (a record
   member by byte offset) and `slot:VER:VRAM8:OLD` (a local or parameter of a function). `VER` is a version alias; leave
   it off for the reference version. A row also carries `name`, one line `doc` of what the code does, `evidence` (at
   least one checkable item; a cited body line counts) and `confidence` (`high`, or `low` to ask for review).
2. **Submit and read the answer at once.** `decomp recipe name submit --ledger LANE rows.jsonl` judges every row in
   seconds against the tree as it is plus the rows already queued, writes nothing to any tree, and queues the accepted
   ones. A refusal prints `refused RULE KEY -> NAME: reason`; fix and resubmit only those rows. Do not wait for a
   landing: take the next pack. Rows sent back later (the applier could not prove them) are printed first by your
   next submit. A row for a target outside your slice is refused (`owner`); send what you learn about it as a row with
   `"suggest": true`, which its owner sees and nothing applies by itself.
3. **The train lands batches.** About every five minutes, or at 300 queued rows, one applier applies every queued row as
   a single token map on a warm tree, proves it with `unbake renameproof` (no build), commits the batch and writes
   its old-to-new map for the port. A row the proof cannot clear is split out and returned; it is never built inside
   the batch. Every sixth batch also runs the full ROM check as a canary.
4. **A name is fixed forward.** A bad name is replaced by a new row for the same key, never reverted. A reviewer samples
   the landed batches and every `low` confidence row.
5. **Steer by numbers.** `decomp cleanup count --distinct` is the progress number (different names left, per class and
   folder; a pass removes names, not occurrences). `decomp cleanup ledger --hours 1` shows the queue, batches, rows
   accepted and refused per lane (with the refusing rules) and the share of each slice's functions that has a row.
   A rising refusal share for one rule is a tool gap: fix the tool once, not each lane.
