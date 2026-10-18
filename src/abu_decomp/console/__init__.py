"""Console boundary: the one Textual console and its panes.

Moved here from cake_taste.logs (console, dashboard_model, dashboard_sources, host_output, statusline, wrap,
terminal, terminal_driver, gitrefs); only imports changed. Event feeds go through `feed`, which delegates to the
Cake Taste writer. Panes read published files and CLI JSON only (progress.json, `unbake report --next`, event
feeds, run status, lane registry, PULSE.json and transcripts), never another tool's Python internals.
"""
