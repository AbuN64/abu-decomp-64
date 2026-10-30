# Layout

## Rules
- Generated source layout MUST be generic for any ROM and compiler: .c files MUST be evidence-based modules inferred from shared rodata/data blocks, static-only call clusters and section order.
- Headers MUST be one per module (private types and prototypes) plus a few shared headers grouped by co-usage and one universal base-typedef header; one function per .c or one type per .h MUST NOT be produced.
