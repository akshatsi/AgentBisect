# Progress

Time (IST) · what works · what is next. Posted on X by the human.

- **Mon 14:02 · M0 done.** Scaffold, typed data contracts (`models.py`) and a CLI with four commands. `uv sync`, tests and `--help` pass. Next: the bisect engine, the math, and the LLM client, built in parallel.
- **Mon 14:30 · M1 done.** `agentbisect run` finds the breaking step in a 30-step trace with 5 test runs in the search, and still finds it when the test flips 10% of the time at random (12/12 seeds). The math for flaky tests is in `docs/METHODS.md`. 213 tests pass. Next: record real agents on Alan AI, measure detection lag, HTML report.
- **Mon 15:24 · M2 built, checked offline.** Alan has no API keys, so AgentBisect now records Alan Code (OpenCode in the Alan app) through a plugin that snapshots the repo after every tool call. Detection lag, auto-finding of newly failing tests, and the one-file HTML report all work; 470 tests pass. Next: the first real Alan Code run.
