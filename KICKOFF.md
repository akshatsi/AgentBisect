# Kickoff prompt

Paste this into Claude Code as the first message, from the repo root, after setting the model to
Opus 5.5 at medium effort (`/model`, then `/effort medium`).

---

You are the manager for the AgentBisect hackathon build. Read `CLAUDE.md`, `AGENTS.md`,
`docs/ARCHITECTURE.md`, and `docs/PLAN.md` before doing anything else.

Context:
- This is a 24-hour solo hackathon (Alan AI Openfire). Judging is on build quality, complexity, and whether it works.
  Judges read the README and the commit history.
- The open models run through Alan AI. Credentials are in `.env` (`ALAN_BASE_URL`, `ALAN_API_KEY`, `ALAN_MODEL`).
  The spend cap is `ALAN_SPEND_CAP_USD=8.00`. Never raise it without asking me.
- Your subagents are in `.claude/agents/`: `math-analyst` (Opus), and `core-builder`, `harness-builder`,
  `report-builder`, `verifier` (Sonnet, high effort).

What to do now:
1. Do M0 from `docs/PLAN.md` yourself, including the `models.py` contracts. Commit.
2. Launch M1 with `math-analyst`, `core-builder`, and `harness-builder` in parallel, following the delegation
   rules in `CLAUDE.md` (goal, owned files, interfaces, acceptance check, commit when done).
3. When M1 is back, run `verifier` with M1's acceptance criteria only.
4. Continue milestone by milestone. After each one, write 2–3 lines to `docs/PROGRESS.md` and tell me in one
   short message what works and what's next.

Stop and ask me before:
- spending real Alan credits beyond a single cost-measuring run,
- changing scope or the CLI surface,
- cutting anything that `docs/PLAN.md` says never to cut.

Start with M0.
