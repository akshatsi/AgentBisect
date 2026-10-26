# Decisions

One line per non-obvious choice: date, decision, reason.

- 2026-10-26 · Step indices are 1-based; step 0 is the base snapshot. `Trace.snapshot_at(i)` falls back to the latest earlier snapshot, so read-only steps need none.
- 2026-10-26 · A run on disk is `meta.json` + `trace.jsonl` (one `Step` per line), so traces stream and append cleanly.
- 2026-10-26 · Ruff ignores E501 (the formatter owns line length) and B008 (typer's `Option()` defaults).
- 2026-10-26 · Flaky oracle: a sequential ±d test with an odd run cap, per-decision error budget 0.05 / (⌈log₂ n⌉ + 2). See METHODS §2.
- 2026-10-26 · Deterministic oracle is assumed when 6 calibration runs all agree, then the boundary gets 3 fresh runs on each side; any disagreement switches to the flaky path. One run per probe in the common case.
- 2026-10-26 · Pytest exit 2 (collection error, e.g. a syntax error the agent wrote) counts as FAIL, not ERROR, since it is the most common way agents break code.
- 2026-10-26 · In a non-monotone history the decisive step is the last pass→fail transition (the break the final state still carries). AGENTS.md glossary left as written; this refines it.
- 2026-10-26 · Snapshots exclude `.git`, `__pycache__`, `*.pyc`, tool caches, `.venv`, `node_modules`: they never affect test results but would make identical states look different. Dedup is by tree hash across the whole history, so a reverted edit reuses the earlier snapshot SHA.
- 2026-10-26 · The oracle runs with `PYTEST_ADDOPTS=--rootdir=<scratch>` and the venv's bin dir first on PATH, so a project config above `runs/` (this repo's own `pyproject.toml`) cannot leak into the check.
- 2026-10-26 · `bisect()` takes an optional `executor` (sha -> OracleRun) so tests simulate flaky oracles with no subprocess. Real runs never use it.
