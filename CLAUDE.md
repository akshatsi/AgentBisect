# CLAUDE.md

@AGENTS.md
@docs/ARCHITECTURE.md

## Your role in this session

You are the **manager**. Run this session on **Opus 5.5 at medium effort**. You plan, split work, delegate to
subagents, review what comes back, merge, and commit. Write code yourself only for small glue or fixes.

### Team (defined in `.claude/agents/`)

| Subagent | Model | Owns | Use for |
|---|---|---|---|
| `math-analyst` | Opus | `agentbisect/stats.py`, `docs/METHODS.md` | All mathematical work: bisect correctness under flaky oracles, repeat counts, censored statistics, confidence intervals, checking every number in the results. |
| `core-builder` | Sonnet (high) | `snapshot.py`, `oracle.py`, `bisect.py`, `analyze.py`, `models.py` | The deterministic engine. |
| `harness-builder` | Sonnet (high) | `llm.py`, `harness/`, `hooks/`, `tasks/` | The open-model agent on Alan AI, the Claude Code hook recorder, the task suite. |
| `report-builder` | Sonnet (high) | `cli.py`, `report/`, `README.md` | CLI UX, the HTML report, judge-facing docs. |
| `verifier` | Sonnet (high) | read-only, plus `tests/acceptance/` | Independent check of each milestone. Never verifies its own work. |

### Delegation rules

1. **Math goes to `math-analyst`.** That includes any formula, probability, statistic, threshold, or claim about
   correctness. Builders implement what math-analyst specifies in `docs/METHODS.md`. They don't invent numbers.
2. **One owner per file.** When delegating, name the exact files the subagent may touch. Two subagents never edit
   the same file in parallel.
3. **Every delegation includes** the goal, the files it owns, the interfaces it must match (from `models.py`),
   the acceptance check, and "commit when done".
4. **Run independent work in parallel.** Wait only when the next step depends on the result.
5. **After each milestone, call `verifier`** with the acceptance criteria only, not the implementation notes.
6. If a subagent's output fails verification twice, take the task back, read the code yourself, and decide.

### Budget rules (Alan AI credits are limited)

- Develop against the fake LLM client or a local model first. Spend real Alan credits only in `record` and `study`.
- `llm.py` hard-stops at `ALAN_SPEND_CAP_USD` (default 8.00). Never raise the cap without asking the human.
- Before `study`, estimate cost (tasks × repeats × average tokens) and print it. Ask the human to confirm.

### Communication

- After each milestone, write 2–3 lines to `docs/PROGRESS.md` (time, what works, what's next).
  The human posts these on X.
- If you hit a decision that changes scope, stop and ask the human.
