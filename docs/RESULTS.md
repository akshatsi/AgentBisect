# Results

**Scope.** These results cover DeepSeek V4.1 Flash (`flash`) running in Alan Code (OpenCode inside
the Alan app), on our practice repo (`tasks/fixture_repo`) and on one real repo
([python-humanize/humanize](https://github.com/python-humanize/humanize)). The human started each
run by hand. The plan (METHODS §8, variant B) had 19 runs. The human stopped the study early,
after 6 counted runs: 4 task runs, 2 planted-break runs. The order changed once: s03 (a
planted-break run) moved ahead of t04 so we would get a broken trace early
(`docs/STUDY_RUNS.md`, `docs/DECISIONS.md`). Nothing here describes other models, other agents
or other code.

## Headline numbers

| Metric | Value | 95% interval | n | Method |
|---|---|---|---|---|
| Task runs that ended with a failing test | 0 of 4 | 0%–49% | 4 | Wilson |
| Planted breaks where bisect named the planted step | 2 of 2 | 34%–100% | 2 | Wilson |
| Detection lag (Kaplan–Meier median) | not estimable | – | 0 | needs ≥ 6 broken runs, ≥ 3 seen (METHODS §8.4) |
| Share of runs where the agent never saw the break | not estimable | – | 0 | no broken task run |
| Tokens spent after the break | not estimable | – | 0 | no broken task run |
| Seen-and-fixed breaks in task runs | 0 | – | 4 runs | METHODS §8.4 |

The lag statistics cannot be estimated because no task run ended broken, and the agent saw no
failure in any of their own test runs either. The four task runs used 1,193,664 tokens in all
(10–21 steps each).

## Simulation evidence (simulation, not real agent runs)

`tests/test_stats.py` runs the full bisect procedure of METHODS §2.6 on 3,000 seeded simulated
histories of 30 steps each, with a random breaking step:

| Oracle flip rate | Wrong step named | Refused as not bisectable |
|---|---|---|
| 0 (deterministic) | 0% (at most 5 probes) | 0% |
| 0.05 | 0.07% | 0.03% |
| 0.10 | 1.1% | 0.2% |
| 0.20 | 2.9% | 4.0% |

This evidence shows the method finds the right step. The planted-break runs below show the
whole pipeline working on real agent traces.

## Case studies

**s03, practice repo, planted break.** At step 9 we planted a bug in `shop/orders.py`:
cancelling an order restocks 1 unit instead of the ordered quantity.
- The agent's own `pytest` at step 13 showed `test_cancel_restores_stock` failing, a lag of
  **4 steps**.
- The steps after the break (10–13) used **112,179 tokens**, 27% of the run's 422,453.
- At step 18 the agent reverted the file (`git checkout -- shop/orders.py`), and the run ended
  green.
- Bisecting steps 0–13 with that test named step 9.
- Caveat: the planted edit was not the agent's own, so it found it with `git diff` (step 15).
  An agent hunting its own bug has no such help, so this lag may be shorter than an organic one.

**humanize, real repo, planted break.** At step 8, the agent's last step, we planted a bug in
`src/humanize/filesize.py`.
- Auto-oracle found 3 tests that pass at step 0 and fail at the end. Bisect named step 8 for
  each one.
- Each search used 4 oracle decisions: 2 endpoints and 2 bisect probes. Each took 14 pytest runs,
  including calibration and the 3 + 3 boundary confirmation runs.
- Stated confidence was 99.67% (deterministic oracle, METHODS §2.5).
- The agent had **no chance to see the break**, since nothing came after it. The run counts for
  bisect accuracy only (METHODS §4.4).

**humanize, real repo, real task (not counted in the metrics).** The task: fix `ordinal()` for
negative numbers and add a `sep` argument to `intcomma`.
- 70 steps and 2,501,627 tokens. The run ended green, and AgentBisect correctly reported no
  break.
- One agent `pytest` run (step 23) showed "15 errors". These came from benchmark tests that the
  agent's `-p no:benchmark` flag broke, not from a code change, so they are rightly not counted
  as a break.
- This run found real-repo bugs in AgentBisect itself, all now fixed:
  - pytest colour codes hid every result line;
  - snapshots dropped humanize's git-ignored `_version.py`, so no restored state could import;
  - editable installs had to be checked so the oracle tests the snapshot, not the agent's
    working copy.

## What we can and cannot claim

- **Can:** AgentBisect named the planted step in 2 of 2 real agent traces. In simulation it names
  the right step in at least 97% of runs, at oracle flip rates up to 0.2.
- **Can:** in 4 practice-repo tasks, Flash broke no existing test that it left broken (0/4; the
  true rate could be as high as 49%).
- **Can:** in one planted case the agent took 4 steps and 112k tokens to see a break.
- **Cannot:** say how often agents break code, how long they take to notice in general, or how
  many tokens they waste. Four runs with no organic break support no such number.
- **Cannot:** compare models. Only one model was run.

## Reproduce

No API key is needed; the traces and snapshots are in `runs/`.

```bash
uv sync
uv run agentbisect study --runs runs                                 # writes runs/study/study.json, study.html
uv run agentbisect run --trace runs/s03-flash-20261026-135147        # seen-and-fixed break: step 9
uv run agentbisect run --trace runs/humanize-sabotage-flash          # planted break: step 8, 3 tests
uv run agentbisect report --trace runs/humanize-sabotage-flash       # HTML report
uv run pytest -q tests/test_stats.py                                 # simulation and statistics
```

Audited by math-analyst: 0 mismatches in `study.json`. Every count, token total, lag and Wilson
interval was recomputed from the run files. Wilson 0/4: upper = z²/(4 + z²) = 0.4899. Wilson
2/2: lower = 2/(2 + z²) = 0.3424.
