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
- 2026-10-26 · `analyze.py` follows METHODS §4 with three tightenings: a key must stand alone in the line (`test_f` does not match `test_f2`), the P2 header uses the class-qualified name when the node ID has a class, and a check that names no test or file (the whole suite) accepts any `FAILED`/`ERROR <id>` line or pytest failure header. Without the last rule a `--check "pytest -q"` run would be censored every time. math-analyst should confirm.
- 2026-10-26 · Auto-oracle runs `python -m pytest -q -rA --continue-on-collection-errors` at step 0 and step n and parses the `-rA` summary. A test that passes at 0 and is FAILED/ERROR at n is bisected with `python -m pytest -q <node id>`; a file that no longer collects takes its passing tests with it; a test missing at n (deleted) is not a failure.
- 2026-10-26 · `bisect_run` returns no culprits when nothing newly fails, and one not-bisectable culprit (check = `python -m pytest -q`) when the suite itself cannot run (exit 127/5/4/3 or timeout at step 0 or n). `RunReport` has no message field, so the CLI words the empty case.
- 2026-10-26 · Not-bisectable reasons name the cause: exit 127 (command not found), 126, pytest 5 (no tests matched), 4 (test ID not found or usage error), 3 (internal error), or timeout with the limit. The prefix `oracle error at step N:` stays.
- 2026-10-26 · Alan AI has no API-key access, so the agent under test is Alan Code (OpenCode bundled in the Alan app, open models via Alan). A project plugin in the task folder records every tool call and snapshots the repo. Our own harness on `llm.py` stays as a backup for any OpenAI-compatible endpoint. Checked: OpenCode 1.19.3 loads `<project>/.opencode/plugin/*.js`.
- 2026-10-26 · Because runs are started by hand and billed to the human's Alan account, the study shrinks to about 16–20 runs (design in METHODS §8). The `ALAN_SPEND_CAP_USD` cap applies only to the backup harness.
- 2026-10-26 · Snapshots also exclude `.opencode/` (the Alan Code recorder plugin lives at `<workdir>/.opencode/plugin/agentbisect.js`), so it never shows in snapshots, diffs, tree dedup or the oracle scratch tree.
- 2026-10-26 · `bisect`, `bisect_run`, `analyze` and `Oracle` resolve `run_dir` (and the scratch dir) to absolute paths on entry: a relative path was resolved a second time inside the check's shell, so pytest looked in the wrong directory.
- 2026-10-26 · Claude Code hook registers `PostToolUseFailure` too: a Bash command with non-zero exit (a failing pytest) fires only that event, so without it no failure output is recorded. Base snapshot comes from `SessionStart`.
- 2026-10-26 · Tokens of one model request go to its first tool call only, in every recorder. `tokens_in` includes cache read and write.
- 2026-10-26 · Alan Code runs fall back to rebuilding steps from `events.jsonl` with one final snapshot if `tool.execute.after` never fires. Such runs are smoke tests, not study data. `agentbisect run` finalizes an Alan Code run first, in case the agent never went idle.
- 2026-10-26 · `record()` re-raises `SpendCapExceeded` after saving the partial trace, so a batch stops when the cap is hit.
- 2026-10-26 · Each Alan Code workdir gets its own git repo with one commit. The first real run showed the agent running `git status`, which walked up into the user's project repo. Snapshots exclude `.git`.
- 2026-10-26 · First real Alan Code run (t01, DeepSeek V4.1 Flash): per-call hooks fire, 21 steps each with a snapshot. Alan's events report cost 0, so cost per run is read from the Alan usage page, not the trace. One run used about 460k tokens, mostly re-sent context.
