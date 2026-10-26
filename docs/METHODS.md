# Methods

Owned by `math-analyst`. Builders implement what this file specifies and invent no numbers.
Every constant named in `CAPS` lives in `agentbisect/stats.py`; code imports it from there.

Notation: the trace has steps `1..n`; step 0 is the base state. `S(i)` is the snapshot after step
`i` (`Trace.snapshot_at(i)`). `O(i)` is the oracle's true verdict on `S(i)`, PASS or FAIL.

---

## 1. Bisect over a monotone history

### 1.1 Candidates

Only steps that change the repo state can be decisive. The **candidates** are the step indices
`c_1 < c_2 < … < c_m` with `S(c_j) ≠ S(c_j − 1)`, compared by tree hash (`Step.tree`). Put
`c_0 = 0` (the base). Read-only steps and steps that leave the tree unchanged are never
candidates, so `m ≤ n`. Every step `i` with `c_j ≤ i < c_{j+1}` has the same state as `c_j`.

### 1.2 Monotone assumption and invariant

A history is **monotone** if there is one `t` with `O(i) = PASS` for `i < t` and `O(i) = FAIL`
for `i ≥ t`. Then `t` is the decisive step `t_err`, and it is a candidate.

Binary search keeps two candidate positions `lo < hi` (indices into `c_0..c_m`) with the
**invariant** `O(c_lo) = PASS` and `O(c_hi) = FAIL`. It starts at `lo = 0`, `hi = m`, which the
preconditions (§2.6 steps 1–3) establish.

```
lo, hi = 0, m
while hi - lo > 1:
    mid = (lo + hi) // 2
    if probe(c_mid) == FAIL: hi = mid
    else:                    lo = mid
return t_err = c_hi            # the boundary pair is (c_lo, c_hi)
```

When the loop ends, `hi = lo + 1`, so `c_hi` is the first candidate whose state fails. Because
every step between `c_lo` and `c_hi` shares `c_lo`'s state, `c_hi` is the first failing step.

### 1.3 What counts as a probe

A **probe** is one oracle *decision* about one snapshot. A decision may use several oracle
*runs* (§2). Re-running an already decided snapshot (the confirmation in §2.5) adds runs to
that probe; it is not a new probe. The two endpoint decisions (step 0 and step n) are the
**precondition probes** and are counted separately (`BisectResult.precondition_probes = 2`).

### 1.4 Probe bound

Each loop iteration replaces the interval length `L = hi − lo` by at most `⌈L/2⌉`. Starting from
`L = m`, the loop runs at most `⌈log₂ m⌉ ≤ ⌈log₂ n⌉` times. For `n = 30`: at most 5 bisect probes,
plus 2 precondition probes, so **7 decisions in total**.

---

## 2. Flaky oracles

### 2.1 What one run means

One run of the check command gives one of three results. This table fixes the classification:

| pytest exit | Result | Why |
|---|---|---|
| 0 | PASS | all selected tests passed |
| 1 | FAIL | a test failed |
| 2 | **FAIL** | collection error (e.g. the agent introduced a syntax or import error). Step 0 passes, so the test collected at the start: an error now is a break the agent caused |
| 3, 4, 5, 126, 127 | ERROR | internal error, bad command line, no tests collected, command not runnable |
| timeout | ERROR | cannot tell a hang from a slow machine |

ERROR runs are **not counted** in `n_fail` or `n_runs` passed to `decide`. If a snapshot
collects `ERROR_RUNS_LIMIT = 2` error runs before `decide` returns, its verdict is ERROR. An
ERROR at step 0 or step n means "not bisectable" (reason: oracle error at that endpoint). An
ERROR at `c_mid` is skipped like `git bisect skip`: probe the untested candidates of
`(lo, hi)` in the order `mid+1, mid−1, mid+2, mid−2, …`, and use the first one that decides. If
every candidate strictly between `lo` and `hi` is ERROR, report "not bisectable" with reason
"steps c_{lo+1}..c_{hi} could not be tested", naming the range.

> Note for core-builder: `oracle.py` currently treats exit 2 as ERROR. It must be FAIL, or
> bisect misses the most common agent break (a syntax error in the code under test).
> `ERROR_RUNS_LIMIT` is a constant to add to `oracle.py`; it is not a statistical quantity.

### 2.2 Flake model and calibration

**Model.** Each run flips the true verdict independently with probability `p < 0.5`, the same
for every snapshot. Runs on one snapshot are i.i.d.

**Calibration** estimates `p` from the two endpoints, whose true verdicts the preconditions fix
(PASS at 0, FAIL at n). An **unexpected** run is a FAIL at step 0 or a PASS at step n.

- **Stage 1:** `CALIBRATION_RUNS = 3` runs at each endpoint (6 runs).
  - All 3 at step 0 FAIL, or all 3 at step n PASS → not bisectable (precondition fails; no
    flake estimate needed).
  - 0 unexpected out of 6 → **deterministic path** (§2.5), `p = 0`.
  - 1 or more unexpected → stage 2.
- **Stage 2:** extend both endpoints to `CALIBRATION_RUNS_FLAKY = 10` runs each (20 runs), then
  `p̂ = flake_rate_from_calibration(k, 20)`, where `k` is the unexpected count over all 20.

**Estimator.** `flake_rate_from_calibration(k, N)` returns 0 if `k = 0`, else the Laplace
estimate `(k + 1) / (N + 2)`, capped at `FLAKE_RATE_CEILING = 0.45`. The Laplace estimate is
the posterior mean under a uniform prior. It never returns 0 once a flip has been seen, and with
`N = 20` it is ≥ 1/11 ≈ 0.09. If `p̂ > MAX_BISECTABLE_FLAKE = 0.40`, report "not bisectable"
(reason: "oracle too flaky: estimated flip rate X"). At `p = 0.4` a single decision already costs
~64 runs on average.

Why 3 and 10: 3 runs per endpoint is the least that lets a majority settle the preconditions and
keeps the deterministic overhead at 6 runs. Stage 2 runs only when flakiness has been seen, and
20 runs keep the estimate from blowing up: with 6 runs a single flip gives `p̂ = 0.25`, which
triples the runs per decision compared with the true rate at `p = 0.1`.

### 2.3 Decision rule: sequential ±d test

For one snapshot, let `D = fails − passes` over all its runs so far. Under the flake model this
is a random walk that steps toward the true verdict with probability `1 − p`. The rule:

```
FAIL  if D ≥ d
PASS  if D ≤ −d
else  run again, until n_runs reaches max_runs (odd); then the sign of D decides
```

This is Wald's sequential probability ratio test for `fail-rate = 1−p` against `fail-rate = p`:
the log-likelihood ratio is `D · ln((1−p)/p)`, so thresholds on the ratio are thresholds on `D`.

**Error without a cap** (gambler's ruin, exact because each step is ±1):

```
e(p, d) = 1 / (1 + r^d),   r = (1 − p) / p
```

**Error with the cap** `M` is computed exactly by dynamic programming over the walk's position
(`stats.sprt_error`). `M` is odd, so the walk cannot sit at 0 at the cap and the sign always
decides. `make_policy` chooses the smallest `d` with `e(p, d) ≤ budget`, then the smallest odd
`M ≥ d` whose exact capped error is `≤ budget` (if none up to `MAX_RUNS_LIMIT = 199`, it tries
`d + 1`).

`decide(policy, n_fail, n_runs)` implements exactly this, always on the **cumulative** counts
for that snapshot (every run ever recorded for it, calibration and confirmation runs included).
It returns non-None whenever `n_runs ≥ max_runs`; a tie past the cap (only possible if runs were
added beyond it) decides FAIL. With `p = 0` the policy is `d = 1, M = 1`: one run decides.

A fixed majority vote needs 7 runs per decision at `p = 0.1` to reach the same budget; the
sequential rule needs 3.7 on average.

### 2.4 Error budget and worked numbers

Bisect returns the right step if every decision is right (monotone history). So by the union
bound, with `K` decisions,

```
P(wrong step) ≤ K · per_probe_error,   K = ⌈log₂ n⌉ + 2,   per_probe_error = target / K
```

`target = TARGET_ERROR = 0.05`. `make_policy(p, n_steps)` uses `n_steps = m` (the candidate
count; `n` also works and is slightly more conservative). For a linear scan (§3) pass
`n_decisions` explicitly.

Worked numbers for `n = 30`: `K = 5 + 2 = 7`, budget `0.05 / 7 = 7.14e−3`.

| p | d | max_runs M | exact error per decision | mean runs per decision | bisect_confidence (7 decisions) |
|---|---|---|---|---|---|
| 0 | 1 | 1 | 0 (assumed) | 1 | 0.9967 (§2.5) |
| 0.05 | 2 | 5 | 3.19e−3 | 2.20 | 0.978 |
| 0.10 | 3 | 7 | 3.31e−3 | 3.69 | 0.977 |
| 0.20 | 4 | 15 | 6.51e−3 | 6.52 | 0.954 |
| 0.30 | 6 | 47 | 7.05e−3 | 14.75 | 0.951 |

Hand check, `p = 0.1, d = 2, M = 3`: wrong if both of the first two runs flip (0.01), or exactly
one flips (0.18) and the third flips (0.1): `0.01 + 0.018 = 0.028`. `e(0.1, 3) = 1/730`.

**Confidence.** `bisect_confidence(policy, K_used) = 1 − K_used · e_exact`, where `K_used` is
the number of decisions actually made, **endpoints included** (`len(probes)` plus
`precondition_probes` if probes excludes them), and `e_exact` is the capped error for the
policy. For `p = 0` it returns `1 − DETERMINISTIC_RESIDUAL` (§2.5).

The guarantee assumes `p̂ ≥ p`. Seeded simulation of the full procedure (`tests/test_stats.py`,
3000 runs, `n = 30`, random `t_err`) gives:

| true p | wrong step | refused (not bisectable) | mean oracle runs per bisect |
|---|---|---|---|
| 0.05 | 0.07% | 0.03% | 30 |
| 0.10 | 1.1% | 0.2% | 41 |
| 0.20 | 2.9% | 4.0% | 63 |
| 0.30 | 5.4% | 27% | 85 |

Up to `p = 0.2` the wrong-step rate stays under 5%. At `p = 0.3` the Laplace estimate is
sometimes below the truth and the rate drifts just past 5%; most such runs are refused anyway.

### 2.5 Deterministic path: "assume 0, confirm the boundary"

When stage 1 sees 0 unexpected runs out of 6, use `make_policy(0.0, m)`: one run per bisect
probe. This alone could hide `p` up to ~0.3 (`0.7^6 = 0.12`), so after bisect returns the
boundary pair `(c_lo, c_hi)`:

1. Add `CONFIRM_RUNS = 3` fresh runs to `S(c_lo)` and 3 to `S(c_hi)` (also when `c_lo = 0` or
   `c_hi = m`). These runs join the existing probes; they are not new probes.
2. If all 3 at `c_lo` PASS and all 3 at `c_hi` FAIL → accept `t_err = c_hi`.
3. Otherwise the oracle is flaky (a deterministic oracle gives the same answer on the same
   tree, monotone or not) → **escalate**: run stage 2 calibration, and compute
   `p̂ = flake_rate_from_calibration(k, N)` where `k` and `N` count unexpected runs and all runs
   over the endpoint runs **plus** the confirmation runs at non-endpoint boundaries (a FAIL at
   `c_lo` or a PASS at `c_hi` is unexpected). Then continue with the flaky path (§2.6 step 4)
   and keep every run recorded so far.

**Why it is safe.** If the accepted step is wrong, exactly one boundary snapshot is in the wrong
state (a FAIL state before a PASS state is impossible in a monotone history), so all 3 of its
fresh confirmation runs flipped. Those runs are independent of everything before them, so

```
P(wrong step accepted on this path) ≤ P(stage 1 sees 0 flips) · p^3 = (1−p)^6 · p^3
```

maximized at `p = 3/9 = 1/3`: `(2/3)^6 (1/3)^3 = 64/19683 = 0.00325 = DETERMINISTIC_RESIDUAL`.
That holds for every `p`, so the deterministic path never needs to know `p`. Cost for `n = 30`:
6 + ≤5 + 6 = ≤17 runs, and exactly 1 run per bisect probe.

### 2.6 The whole procedure (implement this)

```
input: trace, check; candidates c_0=0 < c_1 < ... < c_m          (§1.1)
runs[s] = list of runs per snapshot; decide() always sees cumulative counts

1. stage 1: CALIBRATION_RUNS runs at S(0) and at S(n)
2. if all runs at S(0) FAIL or all at S(n) PASS: NOT_BISECTABLE (precondition)
3. k = fails at S(0) + passes at S(n)
   if k == 0:
       policy = make_policy(0.0, m)
       lo, hi = bisect(policy)                                     (§1.2)
       confirm (§2.5); if agreed: return FOUND c_hi,
                                  confidence = bisect_confidence(policy, decisions)
       else: k_extra, n_extra = confirmation disagreements / runs at non-endpoint boundaries
   else: k_extra = n_extra = 0
4. flaky path:
   extend S(0) and S(n) to CALIBRATION_RUNS_FLAKY runs each
   p̂ = flake_rate_from_calibration(k_endpoints + k_extra, n_endpoints + n_extra)
   if p̂ > MAX_BISECTABLE_FLAKE: NOT_BISECTABLE ("oracle too flaky")
   policy = make_policy(p̂, m)
   decide S(0) and S(n) with policy (keep running until decide() is non-None)
   if S(0) != PASS or S(n) != FAIL: NOT_BISECTABLE (precondition)
   lo, hi = bisect(policy)        # reuses runs already recorded on any snapshot
   return FOUND c_hi, confidence = bisect_confidence(policy, decisions)
5. non-monotone check (§3) on the result
```

Record `policy` in `BisectResult.policy` and `policy.rule` in the bisect log. The CLI gets no
flags for any of this. `OracleConfig.flake_rate` and `max_repeats` are ignored by this procedure
(calibration sets them); leave them for tests that force a policy.

---

## 3. Non-monotone histories

### 3.1 Definitions

A history is **non-monotone** if some `i < j` has `O(i) = FAIL` and `O(j) = PASS` (the agent broke
the test, then fixed it, possibly breaking it again later). A **transition** is a candidate `c_j`
with `O(c_{j−1}) = PASS` and `O(c_j) = FAIL`. The steps `0` and `n` fix that at least one
exists.

**Decisive step in general:** the first step after which the oracle fails *and keeps failing to
the end*, i.e. the **last** transition. That is the break that the final state still carries.
In a monotone history it is the only transition, so this matches the glossary.

Binary search always returns *a* transition (the invariant holds with real verdicts), but in a
non-monotone history it may return an earlier, later-repaired one.

### 3.2 Detection

Binary search alone can never see an inconsistency: every PASS it records lies left of every
FAIL. So detection uses information outside the search.

**Trigger T1 (free, exact): repeated tree across the boundary.** After bisect returns
`(c_lo, c_hi)`, look up the tree hash of every candidate. If some candidate `a ≤ c_lo` and
some candidate `b ≥ c_hi` have the same tree, the history is non-monotone: identical trees get
identical true verdicts, yet monotonicity would need `O(a) = PASS` and `O(b) = FAIL`. This is
common in practice (an agent reverts an edit). Cost: zero oracle runs.

**Trigger T2 (free): decided probe inconsistency.** If any recorded decision has a FAIL at a
candidate left of a PASS (possible after an escalation re-run, or when snapshots share a tree),
the history is non-monotone.

When neither fires, report `non_monotone = False` and `transitions = [t_err]`. Say in the README
limitation list that a non-monotone history without a repeated tree is not detected by default,
which keeps the probe count at `⌈log₂ m⌉`.

### 3.3 Linear confirmation scan (when T1 or T2 fires)

```
policy_scan = make_policy(p_current, m, n_decisions = m + 1)   # every candidate + base
for j in 0..m:                                                   # reuse cached decisions
    v[j] = decide-until-done on S(c_j) with policy_scan
    (a cached decision made under a looser policy is extended with more runs until
     policy_scan decides; with p = 0 the cached single run stands)
transitions = [c_j for j in 1..m if v[j-1] == PASS and v[j] == FAIL]
non_monotone = any(v[i] == FAIL and v[j] == PASS for i < j)      # equivalently len(transitions) > 1
                                                                  # or v[m] == PASS impossible by precondition
t_err = transitions[-1]                                           # last transition (§3.1)
confidence = bisect_confidence(policy_scan, total decisions incl. endpoints)
```

Candidates sharing a tree share one decision (cache by tree hash). If an ERROR candidate sits
between two decided ones, treat it as unknown: a transition across it is reported as the
range, per §2.1.

**Report:** `non_monotone = True`, `transitions` = all transitions in order, `decisive_step =
transitions[-1]`, and in the report a line "the test broke at steps X, Y; it was repaired in
between; the final break is step Y". The detection-lag analysis (§4) uses `transitions[-1]`.

If the scan finds a single transition (T1/T2 fired because of a flip), report `non_monotone =
False` with that transition.

---

## 4. Observed step and detection lag

_In progress (next commit)._

## 5. Censoring and Kaplan–Meier

_In progress (next commit)._

## 6. Confidence intervals (Wilson, bootstrap)

_In progress (next commit)._

## 7. Cost estimate for the study

_In progress (next commit)._
