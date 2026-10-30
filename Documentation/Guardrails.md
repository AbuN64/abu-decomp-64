# Guardrails

## Rules
- Landed decomp C MUST be, apart from vanity (names, comments, formatting), plausibly what the original developers wrote; every guardrail (zero asm, no invented per-function records, casts, pad views, raw offsets, int-as-address, build-time output patching) serves that test.
- A candidate or rule exception MUST be judged by whether the original programmers would plausibly have written it, and a conflict between a rule and this test MUST be raised with the owner.
