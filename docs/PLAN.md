# Build plan — AgentBisect (24 hours: Mon 1 PM → Tue 1 PM IST)

The manager (Opus 5.5, medium) runs this plan. Each milestone ends with a `verifier` check against the
acceptance criteria below, a line in `docs/PROGRESS.md`, and a commit.

## M0 — Scaffold (hour 0–1) · manager

- `uv init`, package layout from `ARCHITECTURE.md`, ruff + pytest config, `.gitignore` (`runs/` except `runs/demo/`).
- Empty `docs/METHODS.md`, `DECISIONS.md`, `PROGRESS.md`, `RESULTS.md`.
- `models.py` interfaces drafted by the manager, so all subagents build against the same contracts.

**Accept:** `uv sync && uv run pytest -q && uv run agentbisect --help` all succeed.

## M1 — Methods and engine (hour 1–6) · in parallel

| Subagent | Work |
|---|---|
| `math-analyst` | `METHODS.md` sections 1–3 first (bisect, flaky oracle, non-monotone) so core-builder is unblocked. Then sections 4–7 and `stats.py`. |
| `core-builder` | `snapshot.py`, `oracle.py`, `bisect.py` against synthetic traces. Waits for METHODS §2 only for repeat counts. |
| `harness-builder` | `llm.py` with spend cap + `FakeLLM`, then `tasks/fixture_repo/` and the task YAMLs. |

**Accept (verifier):**
- On a synthetic trace of 30 steps with a known break at step k, `agentbisect run` finds k for k ∈ {1, 15, 30}.
- Probe count is ≤ ⌈log₂ n⌉ + precondition checks (from the bisect log).
- A simulated flaky oracle (p = 0.1, seeded) still returns the right step.
- The user's real `.git` is untouched (compare `git log` before and after).

## M2 — Recording real agents (hour 6–11) · in parallel

| Subagent | Work |
|---|---|
| `harness-builder` | `harness/agent.py` on Alan AI, sabotage mode, Claude Code hook recorder + `docs/HOOKS.md`. **One** real run to measure cost; report it. |
| `core-builder` | `analyze.py` (t_obs, lag, censoring, wasted tokens), auto-oracle multi-culprit mode. |
| `report-builder` | CLI polish and the HTML report against a recorded fixture trace. |

**Accept:**
- `agentbisect record --task tasks/t01.yaml` produces a trace with one snapshot per mutating step.
- Sabotage run at step k → bisect returns k.
- `agentbisect report` produces one HTML file that opens offline and shows the verdict banner, timeline and diff.

## M3 — The study (hour 11–16) · manager decides, math-analyst audits

1. Manager prints the cost estimate (METHODS §7) and **asks the human to confirm** before spending credits.
2. Run `agentbisect study --suite tasks/ --models qwen,flash,glm,kimi --repeats 1` (10 tasks × 4 models ≈ 40 runs),
   plus ~9 sabotage runs on the cheapest model for bisect accuracy. If the cost estimate exceeds the cap, drop to
   3 models first (keep all tasks), and only then reduce tasks. Never raise repeats until cost is known.
3. `math-analyst` computes the statistics and audits every number.

**Accept:**
- `docs/RESULTS.md` has, overall and per model: runs, failed runs, bisectable share, median lag with 95% CI
  (Kaplan–Meier), censoring rate, wasted-token share, plus sabotage accuracy with a Wilson interval.
- math-analyst states plainly whether the per-model differences are distinguishable at this sample size.
- math-analyst's audit lists zero mismatches.

## M4 — Demo and judge surface (hour 16–21) · report-builder, manager

- Study page with the lag chart, README in the order given in `report-builder.md`.
- Pick the 1–2 most legible real runs for the demo; copy them to `runs/demo/`.
- 20-second GIF of `agentbisect run` for the README.

**Accept:** a fresh clone → `uv sync` → `agentbisect run --trace runs/demo/<id>` → report opens, with no API key.

## M5 — Hardening and submission (hour 21–24) · verifier, manager

- Full verifier pass from a clean clone. Fix only blockers.
- Final `PROGRESS.md` entry, tag `v0.1.0`, make the repo public.
- Human records the 1–2 minute video (script below) and fills in the submission form.

## Cut list (if behind schedule, cut in this order)

1. Claude Code hook recorder (keep the Alan harness only).
2. Non-monotone multi-transition reporting (detect and flag, don't detail).
3. Study page HTML (keep `RESULTS.md`).
4. Never cut: deterministic bisect, sabotage validation, detection-lag numbers, the README.

## Video script (90 seconds)

| Time | Show |
|---|---|
| 0–10s | "An agent worked for 20 minutes and broke the tests. Which step did it?" |
| 10–40s | `agentbisect run` live → verdict box → the HTML report's timeline and culprit diff. |
| 40–55s | Architecture diagram: snapshot every step → bisect with the test → deterministic verdict. |
| 55–80s | The finding: "Across N runs on open models, agents kept working X steps (Y% of tokens) after breaking the code." Plus sabotage accuracy. |
| 80–90s | Repo link, "open models via Alan AI", one-command quickstart. |

## Change of plan (Mon, after M1)

Alan AI offers no API keys. The agent under test is now **Alan Code** (OpenCode inside the Alan app, open models
via Alan), recorded by an OpenCode plugin that `agentbisect record` places in the task folder. The human starts each
run in the Alan app. Consequences:

- M2: one real Alan Code run (started by the human) replaces the "one real run to measure cost".
- M3: about 16–20 hand-started runs instead of about 40. Design and claims in METHODS §8. Sabotage runs stay.
- The own harness on `llm.py` + `FakeLLM` stays as a tested backup for any OpenAI-compatible endpoint.
