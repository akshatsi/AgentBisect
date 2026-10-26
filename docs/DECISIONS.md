# Decisions

One line per non-obvious choice: date, decision, reason.

- 2026-10-26 · Step indices are 1-based; step 0 is the base snapshot. `Trace.snapshot_at(i)` falls back to the latest earlier snapshot, so read-only steps need none.
- 2026-10-26 · A run on disk is `meta.json` + `trace.jsonl` (one `Step` per line), so traces stream and append cleanly.
- 2026-10-26 · Ruff ignores E501 (the formatter owns line length) and B008 (typer's `Option()` defaults).
