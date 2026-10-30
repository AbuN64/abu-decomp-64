# Performance

## Rules
- Every performance mechanism (memo, cache, index, pool path) MUST have a work-count unit test counting reads, scans, compiles or jobs so it cannot be silently undone.
- A change that removes a memo, cache, index or pool path MUST state why it existed.
