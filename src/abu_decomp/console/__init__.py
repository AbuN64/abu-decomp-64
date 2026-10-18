"""Console boundary: the one Textual console and its panes.

Event feeds go through `feed`, which delegates to the
Cake Taste writer. Panes read published files and CLI JSON only (progress.json, `unbake report --next`, event
feeds, run status, lane registry, PULSE.json and transcripts), never another tool's Python internals.
"""
