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
non_monotone = len(transitions) > 1     # same as: some FAIL left of a PASS, since v[0]=PASS, v[m]=FAIL
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

Implemented in `analyze.py` (core-builder). Inputs: the trace, `t_err` (for a non-monotone
history, the last transition, §3.1), the check command, and the culprit diff
(`S(c_lo)` → `S(c_hi)`).

### 4.1 Test key and touched files

- **Node IDs** of the check: every argument of the check command that contains `::`. Strip a
  parameter suffix `[...]`. For each node ID `dir/test_x.py::TestC::test_f` the **key** is the
  part from the file's basename on: `test_x.py::TestC::test_f`. The key matches with any path
  prefix, so it survives a different working directory. The **function name** is the last
  `::` part (`test_f`) and the **class-qualified name** is `TestC.test_f` (or `test_f` alone).
- If the check names only files (no `::`), the key is each file's basename (`test_x.py`).
- **Touched files:** `git diff --name-only` between the boundary snapshots, keeping `*.py`.

### 4.2 Failure signal

Step `s` shows a failure signal if **all** of these hold:

1. `step.kind == SHELL` (file reads and edits do not count, since their output is file text);
2. `step.exit_code != 0` (`None`, meaning unknown, is allowed);
3. one line of `step.output` matches at least one pattern:

| # | Pattern (Python regex, per line) | Example |
|---|---|---|
| P1 | `\b(FAILED\|ERROR)\b` and the line contains the key | `FAILED tests/test_auth.py::test_login - AssertionError` |
| P2 | `^_{3,} (\S+\.)?{function}(\[.*\])? _{3,}$` (pytest's failure header; `{function}` regex-escaped, with the optional class prefix) | `______ TestAuth.test_login ______` |
| P3 | `^_{3,} ERROR collecting \S*{file basename} _{3,}$` | `____ ERROR collecting tests/test_auth.py ____` |
| P4 | `File "([^"]+)", line \d+` where the captured path ends with a touched file | `File "/w/src/auth/session.py", line 41, in login` |
| P5 | `^(\S+\.py):\d+: ` where the captured path ends with a touched file | `src/auth/session.py:41: AssertionError` |

"Ends with" compares whole path components: `a/src/x.py` ends with `src/x.py`, but `ax.py` does
not end with `x.py`. Matching is case-sensitive. `observed_signal` stores the first matching line,
cut to 200 characters.

### 4.3 Definitions

```
t_obs  = min { s : t_err ≤ s ≤ n and step s shows a failure signal }   (None if the set is empty)
lag    = t_obs − t_err                    (if t_obs exists)
censored = t_obs is None
steps_after_err = n − t_err               (exposure; the censor time for §5)
wasted_tokens   = Σ_{s = t_err+1 .. n} (tokens_in(s) + tokens_out(s))
total_tokens    = Σ_{s = 1 .. n} (tokens_in(s) + tokens_out(s))
wasted_share    = wasted_tokens / total_tokens
```

All of `wasted_tokens` were spent while the code was broken: the final state fails (precondition),
so every step after the last transition sits on a broken base.

### 4.4 Edge cases

- **Lag 0.** Step `t_err` itself can be the observing step (e.g. `sed -i … && pytest`).
- **Signals before `t_err`** are ignored; they belong to earlier failures (including repaired
  breaks in a non-monotone history).
- **`t_err = n`** with no signal at step n: censored with `steps_after_err = 0`. It stays in the
  KM risk set at time 0 (it could have shown lag 0 and did not).
- **Multi-culprit runs** (auto-oracle): one analysis per check, each with its own key and its
  own touched files.
- **Truncated output** can hide the pytest summary, which pytest prints last. The recorder should
  keep at least the last 4,000 characters of each output. Truncation biases toward later
  `t_obs` and more censoring, never toward earlier.
- Quiet flags that remove the summary (`-rN`, `--tb=no -qq`) can hide P1–P3; P4–P5 may still
  match. Accept this; it biases the same way.
- **The agent called `finish`** before step n: impossible (finish is the last step); exposure
  always ends at n.

---

## 5. Censoring and Kaplan–Meier

### 5.1 Unit of analysis

Primary: **one row per run.** For a run with several culprits, use the culprit with the smallest
`t_err` (ties: smallest `t_obs`, treating censored as infinite; then the check string in sort
order). Sensitivity analysis: all `(run, culprit)` rows, reported beside the primary, labeled
"rows not independent". Only bisectable runs with status FOUND enter the lag analysis. Sabotage
runs are reported separately (§6.1) and are **not** pooled into the lag analysis, because the
break is injected rather than made by the agent.

Duration and event indicator per row:

```
duration = lag            if observed
duration = n − t_err      if censored   (the agent stopped at step n without seeing it)
event    = not censored
```

Lag is in steps (every step counts, read-only ones too).

### 5.2 Kaplan–Meier (`stats.kaplan_meier`)

At each distinct time `t` with `n_t` rows at risk (duration ≥ t), `d_t` events and `c_t`
censorings at `t` (events before censorings at tied times):

```
S(t) = Π_{u ≤ t} (1 − d_u / n_u)
Greenwood:  G(t) = Σ_{u ≤ t} d_u / (n_u (n_u − d_u))
log-log 95% band:  se = sqrt(G(t)) / |ln S(t)|
                   lower = S(t)^exp(+1.96·se),  upper = S(t)^exp(−1.96·se)
```

The band is (1, 1) while `S = 1`. When `S` drops to 0, the lower bound is 0 and the upper bound
carries forward from the previous time (the band is undefined there; R's `survfit` gives NA).
Read `S(t)` as "share of runs in which the agent had not yet seen the failure `t` steps after
breaking the code". Report `1 − S(0)` as "noticed at once".

### 5.3 Median and its CI (`stats.km_median_ci`)

```
median = first event time with S(t)     ≤ 0.5
lower  = first event time with lower(t) ≤ 0.5
upper  = first event time with upper(t) ≤ 0.5
```

This reads the CI off the pointwise band, as Brookmeyer and Crowley do (R `survival`'s
`quantile.survfit` with `conf.type = "log-log"`; we skip R's midpoint rule when `S` equals 0.5
exactly on a flat stretch). Any value may be **not reached** (`None`): print "not reached", never
a number. A median that is not reached means fewer than half the agents ever noticed.

Hand-checked example (in the tests): durations `1,2,2,3,4,5`, events `1,1,0,1,0,1` →
`S = 0.833, 0.667, 0.444, 0.444, 0`; `G(3) = 1/30 + 1/20 + 1/6 = 0.25`; band at 3 is
`(0.066, 0.785)`; median 3, CI (1, not reached).

### 5.4 Caveat to print with the results

Censoring here is not independent of lag: exposure `n − t_err` is short when the break comes
late in a run with a step cap. KM assumes censoring carries no information about lag. State this
in RESULTS.md next to the median.

---

## 6. Confidence intervals and comparisons

### 6.1 Proportions: Wilson (`stats.wilson_interval`, z = 1.96)

```
center = (p̂ + z²/2n) / (1 + z²/n)
half   = z · sqrt(p̂(1−p̂)/n + z²/4n²) / (1 + z²/n)
```

Clipped to [0, 1]; `n = 0` gives (0, 1). Example: 8/10 → (0.490, 0.943); 0/10 → (0, 0.278).
Used for:

| Quantity | k | n |
|---|---|---|
| bisectable share | runs with status FOUND | runs whose final state fails the oracle |
| censoring rate | censored rows | rows in the lag analysis (§5.1) |
| sabotage accuracy | sabotage runs with `t_err == sabotage_step` | all sabotage runs whose final state fails (not-bisectable counts as wrong) |
| failed-run share | runs whose final state fails | all runs |

### 6.2 Seeded bootstrap (`stats.bootstrap_ci`)

For the **wasted-token share**, report the pooled ratio `Σ wasted_tokens / Σ total_tokens` over
the rows of §5.1. CI: resample rows with replacement, `B = 2000`, `seed = 0`, percentile method;
quantiles by nearest rank: lower = `r[⌊B·0.025⌋]` = `r[50]`, upper = `r[⌈B·0.975⌉ − 1]` = `r[1949]`
of the sorted replicates. Also report the median per-run share, without a CI.

### 6.3 Are per-model differences distinguishable?

Decide with **one global test per quantity, α = 0.05**, chosen before seeing the data:

- **Lag:** k-sample log-rank test across models (`stats.logrank_test`), χ² with k − 1 df. It uses
  the censored rows correctly.
- **Proportions** (censoring rate, bisectable share, failed-run share): permutation test on the
  Pearson χ² of the 2×k table (`stats.proportion_test`, 10,000 permutations, seed 0). It stays
  valid at small counts, where the χ² approximation does not.

If the global p ≥ 0.05, write: "**not distinguishable at this sample size** (log-rank p = …)",
and show the per-model medians with CIs for description only. If p < 0.05, run pairwise tests
with Holm's correction (sort the m pairwise p-values ascending; reject the i-th (1-based) while
`p_i ≤ 0.05 / (m − i + 1)`; stop at the first failure). Never claim a difference from
non-overlapping or overlapping CIs alone.

Expect "not distinguishable": with ~10 runs per model, a Wilson interval on a proportion is
about ±0.3 wide, so only differences larger than about 0.5 in a rate can show.

---

## 7. Cost estimate for the study

### 7.1 Formula (`stats.estimate_study_cost`)

```
runs_m  = tasks × repeats + extra_m          (extra_m: e.g. 9 sabotage runs on the cheapest model)
cost_m  = runs_m × (Ī × in_price_m + Ō × out_price_m) / 1e6        prices in USD per 1M tokens
total   = Σ_m cost_m  (+ explainer: one model row with runs = expected culprits)
print     total × 1.25 (margin) and compare with the remaining budget
          (ALAN_SPEND_CAP_USD − spent so far), passed as cap_usd
```

`Ī`, `Ō` are tokens **per agent run** (summed over steps), from the one measured run in M2.
Scaling: if the measured run took `s₁` steps and tasks allow `max_steps = s`, use
`Ī × (s/s₁)²` and `Ō × (s/s₁)`. Input tokens grow at most quadratically in steps (each call
resends a context that grows by about one step), so this is an upper bound when `s > s₁`.
For the explainer use `Ī = 2,000`, `Ō = 200` per culprit (≤120 words out plus a diff in).

### 7.2 Decision rule (from PLAN.md M3)

If `with_margin_usd > cap_usd`: drop to 3 models (drop the most expensive per run), keep all
tasks; recompute; only then cut tasks. Never raise repeats before cost is known. Print the
estimate and ask the human before spending.

### 7.3 Prices (fill in after the model probe; source: `docs/DECISIONS.md`)

| Alias | API model ID | USD / 1M in | USD / 1M out |
|---|---|---|---|
| qwen | _TBD_ | _TBD_ | _TBD_ |
| flash | _TBD_ | _TBD_ | _TBD_ |
| glm | _TBD_ | _TBD_ | _TBD_ |
| kimi | _TBD_ | _TBD_ | _TBD_ |
| explainer (Nemotron 3.5 Lightning) | _TBD_ | _TBD_ | _TBD_ |

Worked example with made-up prices (in the tests): 10 tasks × 1 repeat, model a at
$0.10/$0.40 with 9 extra runs, model b at $1.00/$3.00, `Ī = 200k`, `Ō = 10k` →
a: 19 × $0.024 = $0.456; b: 10 × $0.23 = $2.30; total $2.756; with margin $3.445.

---

## 8. Study design under a small, hand-run budget

The agent under test is Alan Code (OpenCode in the Alan app). The human starts each run by hand,
and runs bill the human's Alan account, so the `$8` cap in §7 does not apply. Budget: about
16–20 runs. §7's formula still gives a cost estimate once per-run prices are known.

### 8.1 Design: 2 models × 8 tasks + 3 sabotage runs = 19 runs

- **Models: `qwen` (Qwen3.8 27B) and `kimi` (Kimi K3).** Two models, not four: with 8 runs each
  a per-model rate has a Wilson interval about ±0.3 wide; with 4 models and 4 runs each it
  would be about ±0.4, which says nothing. Smallest against largest gives the best chance of a
  visible difference on "does a bigger model notice sooner?". A weak model that breaks things
  is useful here, since only broken runs carry lag data. Fallback: if `qwen`'s first run makes
  no tool calls, replace it with `flash` (DeepSeek V4.1 Flash) for all its runs.
- **Tasks: t01, t02, t04, t05, t06, t07, t08, t09.** Dropped: t03 and t10, the two smallest
  single-function changes and the least likely to break an existing test. The 8 kept tasks
  each touch code that existing tests cover (a rename, an enum swap, report formats, the
  coupon table, stock handling).
- **Repeats: 1.** Every run spent on a new task beats a repeat at this n.
- **Sabotage: s01, s02, s03, one run each, on `qwen`** (likely the cheapest). They measure
  bisect accuracy only and stay out of the lag analysis (§5.1).

### 8.2 Run order (seed 20261026)

Each task is a block with one run per model, so a drift over the day (model updates on Alan,
the human's habits) hits both models alike. Block order and model order inside each block are
shuffled; the sabotage runs are spread through the list. Reproduce with
`stats.study_run_order(tasks, ["qwen", "kimi"], [("s01","qwen"), ("s02","qwen"), ("s03","qwen")], seed=20261026)`.

| # | task | model | # | task | model | # | task | model |
|---|---|---|---|---|---|---|---|---|
| 1 | t04 | qwen | 8 | t05 | kimi | 15 | s01 | qwen |
| 2 | t04 | kimi | 9 | t05 | qwen | 16 | t08 | qwen |
| 3 | t01 | qwen | 10 | s02 | qwen | 17 | t08 | kimi |
| 4 | t01 | kimi | 11 | t07 | qwen | 18 | t02 | qwen |
| 5 | s03 | qwen | 12 | t07 | kimi | 19 | t02 | kimi |
| 6 | t06 | qwen | 13 | t09 | qwen | | | |
| 7 | t06 | kimi | 14 | t09 | kimi | | | |

Run them in this order. If a run must be redone (crash, app error before step 1), redo it at
once and note it; never reorder to fill gaps. Slot 20 is a spare for one such redo.

### 8.3 What the study can claim

Pooled over both models (rows per §5.1):

| Claim | Method | Example at this n |
|---|---|---|
| Broken-run share: X of 16 runs ended with a failing test | Wilson (§6.1) | 6/16 → 0.38 (0.18–0.61) |
| Censoring rate: the agent never saw the break in C of K broken runs | Wilson | 3/6 → 0.50 (0.19–0.81) |
| Median detection lag in steps | KM median, log-log CI (§5.3) | expect a wide CI, often "upper: not reached" |
| Wasted-token share after the break | pooled ratio, seeded bootstrap (§6.2) | with K < 10 the percentile CI is too narrow; say so |
| Bisect accuracy: injected step found in 3 of 3 sabotage runs | Wilson | 3/3 → **(0.44, 1.00)** |

3/3 correct only shows accuracy is probably above 44%. The strong accuracy evidence is the
simulation (§8.5).

**Per-model results are descriptive only.** Report them in a table without a winner. Smallest
differences that could reach p < 0.05:

- **Proportions, 8 vs 8 runs:** 0/8 against 5/8 (Fisher two-sided p = 0.026); 0/8 against 4/8
  gives p = 0.077. So only a gap of about **60 percentage points** can show.
- **Lag:** with 3 or fewer broken runs per model, no two-sided rank test can reach p < 0.05,
  even when every lag of one model is shorter than every lag of the other (smallest possible
  p = 2/20 = 0.10). With 4 against 4, only complete separation reaches it (p = 2/70 = 0.029).
  Run the global tests of §6.3 anyway and print the p-value with the words "not
  distinguishable at this sample size" unless it is below 0.05.

**Cannot claim:** that one model notices sooner, any ranking of models, or that the numbers
hold beyond this fixture library and these 8 tasks.

### 8.4 Runs that end green, and when to report the lag

- A run whose final state passes the oracle has **no break**. It counts in the denominator of
  the broken-run share (§8.3, first row), is not bisected, and enters no lag statistic.
- **Self-observed breaks (free salvage, reported separately).** If a green run contains an agent
  `pytest` step `s` whose output shows a failure signal (§4.2) for a test that passes at step 0,
  bisect over steps `0..s` with that test as the oracle (precondition: pass at 0, fail at `s`).
  This gives `t_err` and a lag, with no new Alan runs. Label these "breaks the agent saw and
  repaired". **Never pool them** with the main lag analysis: they are found *because* the agent
  saw them, so they are never censored and bias the lag downward.
- **Threshold: report the pooled KM median only if there are at least 6 broken, bisectable
  runs (status FOUND) and at least 3 of them are observed.** Reasons: KM needs S(t) ≤ 0.5 to
  produce a median, which takes at least half the rows observed; with 6 rows the censoring-rate
  Wilson interval is still about ±0.3 wide, the least that tells "most agents notice" apart from
  "most do not"; below 6, one run moves the median by a large step.
- **Below the threshold:** report each broken run as a row (task, model, `t_err`, `t_obs` or
  "never", lag, wasted tokens) and the censoring count as k/K with its Wilson interval. No
  median. Topping up with more runs needs the human's approval; if approved, add runs on the
  tasks that broke most often, alternating models, and say in RESULTS.md that the top-up was
  chosen after seeing the data.

### 8.5 Free supplementary evidence: simulation

`tests/test_stats.py` runs the full §2.6 procedure on simulated monotone histories (n = 30,
random `t_err`, 3000 runs per setting, seed 20261026). Results (§2.4): with a deterministic
oracle the right step every time, using at most ⌈log₂ 30⌉ = 5 probes; with flip rate 0.1, the
wrong step in 1.1% of runs; with 0.2, in 2.9%. Report this beside the 3/3 sabotage result,
**labeled "simulation, not real agent runs"**. It shows that the method is correct; the
sabotage runs show that the pipeline works on real traces.
