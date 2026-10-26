# Progress

Time (IST) · what works · what is next. Posted on X by the human.

- **Mon 14:02 · M0 done.** Scaffold, typed data contracts (`models.py`) and a CLI with four commands. `uv sync`, tests and `--help` pass. Next: the bisect engine, the math, and the LLM client, built in parallel.
- **Mon 14:30 · M1 done.** `agentbisect run` finds the breaking step in a 30-step trace with 5 test runs in the search, and still finds it when the test flips 10% of the time at random (12/12 seeds). The math for flaky tests is in `docs/METHODS.md`. 213 tests pass. Next: record real agents on Alan AI, measure detection lag, HTML report.
- **Mon 15:24 · M2 built, checked offline.** Alan has no API keys, so AgentBisect now records Alan Code (OpenCode in the Alan app) through a plugin that snapshots the repo after every tool call. Detection lag, auto-finding of newly failing tests, and the one-file HTML report all work; 470 tests pass. Next: the first real Alan Code run.
- **Mon 19:28 · Works on your own repo.** `agentbisect record --repo <path or GitHub URL> --prompt "..."` copies the project, installs its dependencies into a separate venv, and records the agent; bisect then runs tests in that project's own Python. The original repo is never touched. 4 real Alan Code study runs so far, all left the tests green. 544 tests pass.
- **Mon 23:50 · Results and demo done.** On a real repo (python-humanize), AgentBisect named the exact step of a planted break; in a practice run it showed the agent broke a test at step 9, saw it at 13 and fixed it. DeepSeek Flash left no test broken in 4 task runs. Every number reproduces from a fresh clone with no API key (`runs/demo`). 597 tests pass.
- **Tue 00:02 · v0.1.0 tagged.** A final check from a clean clone of GitHub passed all 8 points (README commands, demo verdicts, every results number, offline reports, 597 tests, lint, no secrets). Next: the demo video and the submission.
- **Tue 01:16 · Interactive UI.** Reports are now one offline page with a bisect replay you can play, a step explorer with diffs, and a study landing page. 638 tests pass. Tagged v0.2.0.
