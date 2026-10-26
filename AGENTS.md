# AGENTS.md — AgentBisect

Instructions for any coding agent working in this repo (Claude Code, Codex, OpenCode, Cursor).

## What we are building

**AgentBisect** finds the exact step where a coding agent broke your code. It works like
`git bisect`, but over the agent's individual actions instead of commits.

A coding agent works for 20 minutes, makes 30 edits, and the tests go red. Today you see only the
final error. AgentBisect records a snapshot after every agent action, binary-searches those
snapshots with your failing test, and reports:

> **Step 9** (`edit src/auth/session.py`) broke `test_login`.
> The agent only noticed at **step 21**, so it spent 12 steps and 41k tokens working on top of a broken base.

The verdict is **deterministic**: it comes from running tests, never from an LLM's opinion. An LLM
is used only to write a short, clearly labeled explanation.

**The extension (our research angle):** across many agent runs, we measure the **detection lag**,
meaning how many steps pass between the step that broke the code (*decisive step*) and the first
step where the agent could see the failure (*observed step*). We also measure how many tokens were
spent in between. This turns the tool into a finding.

## Glossary (use these words exactly, in code and docs)

| Term | Meaning |
|---|---|
| **trace** | The ordered list of steps from one agent run, stored as JSONL. |
| **step** | One agent tool call (read, write, edit, shell), with its arguments, output, and the agent's reasoning before it. |
| **snapshot** | The full repo state after a step, stored as a commit in a *shadow* git repo. |
| **oracle** | The check command that decides pass or fail, e.g. `pytest tests/test_auth.py::test_login -q`. |
| **decisive step** (`t_err`) | The first step after which the oracle fails. Found by bisect. |
| **observed step** (`t_obs`) | The first step at or after `t_err` whose tool output shows the failure to the agent. |
| **detection lag** | `t_obs − t_err`. **Censored** if the agent never observed the failure. |
| **wasted tokens** | Tokens the agent spent after `t_err`. |
| **culprit report** | The final output: decisive step, its diff, the agent's reasoning, lag, and wasted tokens. |

## Commands

```bash
uv sync                                  # install
uv run pytest -q                         # unit tests (must stay green)
uv run ruff check . && uv run ruff format --check .
uv run agentbisect --help
```

## CLI surface (keep it this small)

```bash
agentbisect record  --task tasks/t03.yaml            # run the open-model agent, snapshot every step
agentbisect run     --trace runs/<id> [--check "<cmd>"]   # bisect; auto-detects newly failing tests if no --check
agentbisect report  --trace runs/<id>                # writes runs/<id>/report.html
agentbisect study   --suite tasks/ --models qwen,flash,glm,kimi --repeats 1   # batch runs plus detection-lag statistics per model
```

## Conventions

- Python 3.11, `uv`, `typer` (CLI), `rich` (terminal output), `pydantic` v2 (all data models), `jinja2` (HTML report).
- Every data structure that crosses a module boundary is a pydantic model in `agentbisect/models.py`.
- No global state. Pass config objects explicitly.
- **Never write into the user's real `.git`.** Snapshots go to a shadow repo using
  `git --git-dir=<run>/shadow.git --work-tree=<workdir>`.
- Restoring a snapshot happens in a **scratch worktree** under `runs/<id>/scratch/`, never in the user's directory.
- Every subprocess gets a timeout. Every oracle run records its exit code, duration, and the tail of its output.
- LLM output is never used to decide pass or fail. Label it "AI explanation" wherever it appears.
- All LLM calls go through `agentbisect/llm.py`, which enforces the spend cap.

## Done means

1. Unit tests pass, and the new code has tests (synthetic traces count).
2. `ruff` is clean.
3. `docs/DECISIONS.md` has a line for any non-obvious choice.
4. You committed with a message in the form `area: what changed` (e.g. `bisect: handle flaky oracle with repeated runs`).
   Commit small and often. Judges read the history.

## Do not

- Add a web server, database, or auth. The report is one static HTML file.
- Support languages other than Python, or test runners other than pytest, in v1.
- Let an LLM judge correctness.
- Spend LLM credits in unit tests. Use recorded fixtures or a fake client.
