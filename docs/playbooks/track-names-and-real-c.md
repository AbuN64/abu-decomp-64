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
