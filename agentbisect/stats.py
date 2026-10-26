"""Statistics for AgentBisect. Every constant and rule here is derived in docs/METHODS.md.

Pure standard library plus pydantic. Deterministic: anything random takes an explicit seed.

Sections (match METHODS.md):

- §2 oracle policy: ``CALIBRATION_RUNS``, ``flake_rate_from_calibration``, ``make_policy``,
  ``decide``, ``bisect_confidence``.
- §5 Kaplan–Meier: ``kaplan_meier``, ``km_median_ci``, ``logrank_test``.
- §6 intervals: ``wilson_interval``, ``bootstrap_ci``.
- §7 cost: ``estimate_study_cost``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel

from agentbisect.models import OraclePolicy, Verdict

T = TypeVar("T")

# --------------------------------------------------------------------------- §2 constants

#: Stage-1 calibration: oracle runs at each endpoint (step 0 and step n).
CALIBRATION_RUNS: int = 3

#: Stage-2 calibration: total runs per endpoint once any flakiness has been seen.
CALIBRATION_RUNS_FLAKY: int = 10

#: Fresh runs added to each of the two boundary snapshots when the oracle looks deterministic.
CONFIRM_RUNS: int = 3

#: Default bound on P(bisect names the wrong step) for the whole run.
TARGET_ERROR: float = 0.05

#: Above this assumed flip rate the oracle is reported as too flaky to bisect.
MAX_BISECTABLE_FLAKE: float = 0.40

#: Ceiling on the value returned by ``flake_rate_from_calibration`` (must stay < 0.5).
FLAKE_RATE_CEILING: float = 0.45

#: Hard ceiling on runs per snapshot that ``make_policy`` may choose.
MAX_RUNS_LIMIT: int = 199

#: Worst-case P(wrong step accepted) on the deterministic path (METHODS §2.5):
#: max over p of (1-p)^(2*CALIBRATION_RUNS) * p^CONFIRM_RUNS, attained at p = R / (R + 2C).
_P_STAR = CONFIRM_RUNS / (CONFIRM_RUNS + 2 * CALIBRATION_RUNS)
DETERMINISTIC_RESIDUAL: float = (1 - _P_STAR) ** (2 * CALIBRATION_RUNS) * _P_STAR**CONFIRM_RUNS


def ceil_log2(m: int) -> int:
    """⌈log₂ m⌉ for m ≥ 1 (0 for m = 1)."""
    if m < 1:
        raise ValueError("m must be >= 1")
    return (m - 1).bit_length()


# --------------------------------------------------------------------------- §2 calibration


def flake_rate_from_calibration(n_unexpected: int, n_runs: int) -> float:
    """Flip rate to assume after calibration (METHODS §2.2).

    ``n_unexpected`` counts runs that failed at step 0 or passed at step n, out of ``n_runs``
    calibration runs over both endpoints. Zero unexpected results gives 0.0 (deterministic
    path, which then confirms the boundary with ``CONFIRM_RUNS`` fresh runs). Otherwise the
    Laplace estimate (k + 1) / (n + 2), capped at ``FLAKE_RATE_CEILING``.
    """
    if n_runs < 0 or n_unexpected < 0 or n_unexpected > n_runs:
        raise ValueError("need 0 <= n_unexpected <= n_runs")
    if n_unexpected == 0:
        return 0.0
    return min((n_unexpected + 1) / (n_runs + 2), FLAKE_RATE_CEILING)


# --------------------------------------------------------------------------- §2 SPRT


def sprt_untruncated_error(p: float, d: int) -> float:
    """Error of the ±d random-walk rule with no run cap: 1 / (1 + ((1-p)/p)^d)."""
    if p == 0:
        return 0.0
    r = (1 - p) / p
    return 1.0 / (1.0 + r**d)


def _sprt_scan(p: float, d: int, limit: int) -> list[tuple[int, float, float]]:
    """Exact error and expected runs of the capped ±d rule for every odd cap M ≤ limit.

    By symmetry assume the true state is FAIL: each run moves the walk (fails − passes) by +1
    with probability q = 1 − p and by −1 with probability p. The walk stops at ±d. At the cap
    M (odd, so the walk cannot sit at 0) the sign decides. Returns (M, error, E[runs]).
    """
    q = 1.0 - p
    width = 2 * d - 1  # positions -(d-1) .. d-1
    probs = [0.0] * width
    probs[d - 1] = 1.0  # position 0
    absorbed_wrong = 0.0
    expected = 0.0  # sum over t of P(still running before run t)
    out: list[tuple[int, float, float]] = []
    for t in range(1, limit + 1):
        expected += sum(probs)
        new = [0.0] * width
        for i, mass in enumerate(probs):
            if mass == 0.0:
                continue
            pos = i - (d - 1)
            up, down = pos + 1, pos - 1
            if up >= d:
                pass  # absorbed right: correct
            else:
                new[up + d - 1] += mass * q
            if down <= -d:
                absorbed_wrong += mass * p
            else:
                new[down + d - 1] += mass * p
        probs = new
        if t % 2 == 1 and t >= d:
            wrong_at_cap = sum(m for i, m in enumerate(probs) if i - (d - 1) < 0)
            out.append((t, absorbed_wrong + wrong_at_cap, expected))
    return out


def sprt_error(p: float, d: int, max_runs: int) -> float:
    """Exact per-decision error of the capped ±d rule (``max_runs`` odd)."""
    if p == 0:
        return 0.0
    for m, err, _ in _sprt_scan(p, d, max_runs):
        if m == max_runs:
            return err
    raise ValueError("max_runs must be odd and >= d")


def sprt_expected_runs(p: float, d: int, max_runs: int) -> float:
    """Exact expected runs per decision of the capped ±d rule."""
    if p == 0:
        return 1.0
    for m, _, e in _sprt_scan(p, d, max_runs):
        if m == max_runs:
            return e
    raise ValueError("max_runs must be odd and >= d")


@lru_cache(maxsize=256)
def sprt_params(p: float, budget: float) -> tuple[int, int]:
    """Smallest threshold d, then smallest odd cap M, with exact error ≤ budget.

    Deterministic oracle (p = 0): d = 1, M = 1.
    """
    if not 0.0 <= p < 0.5:
        raise ValueError("flake rate must be in [0, 0.5)")
    if p == 0:
        return 1, 1
    if not 0.0 < budget < 0.5:
        raise ValueError("per-decision budget must be in (0, 0.5)")
    d = 1
    while sprt_untruncated_error(p, d) > budget:
        d += 1
    while True:
        if d > MAX_RUNS_LIMIT:
            raise ValueError(f"no policy within {MAX_RUNS_LIMIT} runs for p={p}")
        for m, err, _ in _sprt_scan(p, d, MAX_RUNS_LIMIT):
            if err <= budget:
                return d, m
        d += 1


def make_policy(
    flake_rate: float,
    n_steps: int,
    target_error: float = TARGET_ERROR,
    n_decisions: int | None = None,
) -> OraclePolicy:
    """Policy such that the whole bisect names the wrong step with probability ≤ target_error.

    Union bound over K oracle decisions, K = ⌈log₂ n_steps⌉ + 2 (bisect probes plus the two
    endpoint decisions) unless ``n_decisions`` is given (e.g. for a linear scan, METHODS §3).
    Each decision gets budget target_error / K and uses the ±d sequential rule (METHODS §2.3).
    """
    if n_steps < 1:
        raise ValueError("n_steps must be >= 1")
    k = n_decisions if n_decisions is not None else ceil_log2(n_steps) + 2
    if k < 1:
        raise ValueError("n_decisions must be >= 1")
    budget = target_error / k
    d, m = sprt_params(flake_rate, budget)
    if flake_rate == 0:
        rule = (
            f"deterministic: 1 run per snapshot; boundary confirmed with {CONFIRM_RUNS} extra runs"
        )
    else:
        rule = (
            f"sequential: stop when |fails - passes| >= {d}; "
            f"cap {m} runs, majority at cap (p={flake_rate:.3f}, budget={budget:.2e})"
        )
    return OraclePolicy(flake_rate=flake_rate, max_runs=m, per_probe_error=budget, rule=rule)


def _threshold(policy: OraclePolicy) -> int:
    if policy.flake_rate == 0:
        return 1
    return sprt_params(policy.flake_rate, policy.per_probe_error)[0]


def decide(policy: OraclePolicy, n_fail: int, n_runs: int) -> Verdict | None:
    """Verdict once decided, ``None`` to request another run (METHODS §2.3).

    Counts are cumulative over every run recorded for this snapshot. FAIL when
    fails − passes ≥ d, PASS when passes − fails ≥ d; at or past ``policy.max_runs`` the
    majority decides and an exact tie (only possible past the cap) decides FAIL.
    """
    if n_runs < 0 or n_fail < 0 or n_fail > n_runs:
        raise ValueError("need 0 <= n_fail <= n_runs")
    if n_runs == 0:
        return None
    diff = n_fail - (n_runs - n_fail)
    d = _threshold(policy)
    if diff >= d:
        return Verdict.FAIL
    if diff <= -d:
        return Verdict.PASS
    if n_runs >= policy.max_runs:
        return Verdict.PASS if diff < 0 else Verdict.FAIL
    return None


def bisect_confidence(policy: OraclePolicy, n_probes: int) -> float:
    """Lower bound on P(decisive step correct) after ``n_probes`` oracle decisions.

    ``n_probes`` counts every decision, endpoints included. Flaky: 1 − n_probes · e, where e is
    the exact per-decision error of the policy's rule. Deterministic: 1 − DETERMINISTIC_RESIDUAL,
    valid only when the boundary confirmation of METHODS §2.5 ran and agreed.
    """
    if n_probes < 0:
        raise ValueError("n_probes must be >= 0")
    if policy.flake_rate == 0:
        return 1.0 - DETERMINISTIC_RESIDUAL
    d = _threshold(policy)
    e = sprt_error(policy.flake_rate, d, policy.max_runs)
    return max(0.0, min(1.0, 1.0 - n_probes * e))


# --------------------------------------------------------------------------- §6 intervals


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n (METHODS §6.1). n = 0 gives (0, 1)."""
    if n < 0 or k < 0 or k > n:
        raise ValueError("need 0 <= k <= n")
    if n == 0:
        return 0.0, 1.0
    phat = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap_ci(
    items: Sequence[T],
    statistic: Callable[[list[T]], float],
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Seeded percentile bootstrap CI: resample ``items`` with replacement (METHODS §6.2).

    Quantiles use the nearest-rank rule on the sorted replicates: lower = r[⌊B·α/2⌋],
    upper = r[⌈B·(1−α/2)⌉ − 1].
    """
    if not items:
        raise ValueError("need at least one item")
    rng = random.Random(seed)
    data = list(items)
    size = len(data)
    reps = sorted(
        statistic([data[rng.randrange(size)] for _ in range(size)]) for _ in range(n_boot)
    )
    lo = reps[int(math.floor(n_boot * alpha / 2))]
    hi = reps[int(math.ceil(n_boot * (1 - alpha / 2))) - 1]
    return lo, hi


# --------------------------------------------------------------------------- §5 Kaplan–Meier


class KMPoint(BaseModel):
    """Kaplan–Meier curve just after ``time`` (METHODS §5.2)."""

    time: float
    at_risk: int
    events: int
    censored: int
    survival: float
    greenwood: float  # running sum of d / (n (n - d)); inf once survival hits 0
    lower: float
    upper: float


def _loglog_bounds(s: float, gw: float, z: float) -> tuple[float, float]:
    if s >= 1.0:
        return 1.0, 1.0
    if s <= 0.0 or math.isinf(gw):
        return 0.0, 0.0
    se = math.sqrt(gw) / abs(math.log(s))
    return s ** math.exp(z * se), s ** math.exp(-z * se)


def kaplan_meier(
    durations: Sequence[float], observed: Sequence[bool], z: float = 1.96
) -> list[KMPoint]:
    """Kaplan–Meier survival curve with Greenwood log-log pointwise bands (METHODS §5.2).

    One point per distinct time (events or censorings), in increasing order. At tied times,
    events happen before censorings. ``survival`` is S(t) just after ``time``.
    """
    if len(durations) != len(observed):
        raise ValueError("durations and observed must have equal length")
    if any(t < 0 for t in durations):
        raise ValueError("durations must be >= 0")
    times = sorted(set(durations))
    at_risk = len(durations)
    s, gw = 1.0, 0.0
    curve: list[KMPoint] = []
    for t in times:
        d = sum(1 for x, o in zip(durations, observed, strict=True) if x == t and o)
        c = sum(1 for x, o in zip(durations, observed, strict=True) if x == t and not o)
        if d:
            s *= 1 - d / at_risk
            gw = math.inf if d == at_risk else gw + d / (at_risk * (at_risk - d))
        lo, hi = _loglog_bounds(s, gw, z)
        if s <= 0.0 and curve:
            hi = curve[-1].upper  # band undefined at S = 0: carry the upper bound forward
        curve.append(
            KMPoint(
                time=t,
                at_risk=at_risk,
                events=d,
                censored=c,
                survival=s,
                greenwood=gw,
                lower=lo,
                upper=hi,
            )
        )
        at_risk -= d + c
    return curve


class MedianCI(BaseModel):
    """Median of a survival curve with its 95% CI. ``None`` means "not reached"."""

    median: float | None
    lower: float | None
    upper: float | None
    n: int
    events: int


def km_median_ci(durations: Sequence[float], observed: Sequence[bool], z: float = 1.96) -> MedianCI:
    """KM median and Brookmeyer–Crowley-style CI from the log-log band (METHODS §5.3).

    median = first time with S(t) ≤ 0.5; lower = first time with the lower band ≤ 0.5;
    upper = first time with the upper band ≤ 0.5. Each is ``None`` if never reached.
    """
    curve = kaplan_meier(durations, observed, z)

    def first(attr: str) -> float | None:
        for pt in curve:
            if getattr(pt, attr) <= 0.5:
                return pt.time
        return None

    return MedianCI(
        median=first("survival"),
        lower=first("lower"),
        upper=first("upper"),
        n=len(durations),
        events=sum(1 for o in observed if o),
    )


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a·x = b by Gaussian elimination with partial pivoting (small k)."""
    k = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular covariance matrix")
        m[col], m[piv] = m[piv], m[col]
        for r in range(k):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, k + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][k] / m[i][i] for i in range(k)]


def _gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) (series / continued fraction)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(1000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return 1.0 - total * math.exp(-x + a * math.log(x) - gln)
    tiny = 1e-300
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_sf(x: float, df: int) -> float:
    """Upper tail P(χ²_df ≥ x)."""
    if df < 1:
        raise ValueError("df must be >= 1")
    return _gamma_q(df / 2, x / 2)


class LogRankResult(BaseModel):
    chi2: float
    df: int
    p_value: float


def logrank_test(groups: dict[str, tuple[Sequence[float], Sequence[bool]]]) -> LogRankResult:
    """k-sample log-rank test of equal lag distributions (METHODS §6.3).

    ``groups`` maps a label to (durations, observed). Statistic (O−E)ᵀ V⁻¹ (O−E) over the first
    k−1 groups, χ² with k−1 degrees of freedom.
    """
    labels = sorted(groups)
    k = len(labels)
    if k < 2:
        raise ValueError("need at least two groups")
    data = [(lab, t, o) for lab in labels for t, o in zip(*groups[lab], strict=True)]
    event_times = sorted({t for _, t, o in data if o})
    o_minus_e = [0.0] * k
    var = [[0.0] * k for _ in range(k)]
    for t in event_times:
        n_g = [sum(1 for lab, x, _ in data if lab == g and x >= t) for g in labels]
        d_g = [sum(1 for lab, x, o in data if lab == g and x == t and o) for g in labels]
        n, d = sum(n_g), sum(d_g)
        for i in range(k):
            o_minus_e[i] += d_g[i] - d * n_g[i] / n
        if n > 1:
            f = d * (n - d) / (n - 1)
            for i in range(k):
                for j in range(k):
                    delta = 1.0 if i == j else 0.0
                    var[i][j] += f * (n_g[i] / n) * (delta - n_g[j] / n)
    u = o_minus_e[: k - 1]
    v = [row[: k - 1] for row in var[: k - 1]]
    x = _solve(v, u)
    chi2 = sum(ui * xi for ui, xi in zip(u, x, strict=True))
    return LogRankResult(chi2=chi2, df=k - 1, p_value=chi2_sf(chi2, k - 1))


class ProportionTest(BaseModel):
    chi2: float
    p_value: float
    n_perm: int


def _pearson_chi2(groups: list[tuple[int, int]]) -> float:
    k_tot = sum(k for k, _ in groups)
    n_tot = sum(n for _, n in groups)
    if n_tot == 0 or k_tot in (0, n_tot):
        return 0.0
    rate = k_tot / n_tot
    chi2 = 0.0
    for k, n in groups:
        if n == 0:
            continue
        e1, e0 = n * rate, n * (1 - rate)
        chi2 += (k - e1) ** 2 / e1 + ((n - k) - e0) ** 2 / e0
    return chi2


def proportion_test(
    groups: dict[str, tuple[int, int]], n_perm: int = 10_000, seed: int = 0
) -> ProportionTest:
    """Permutation test that k groups share one proportion (METHODS §6.3).

    ``groups`` maps a label to (successes, trials). Statistic: Pearson χ² of the 2×k table.
    Labels are shuffled ``n_perm`` times with a seeded RNG; p = (1 + #{χ²_perm ≥ χ²}) / (1 + n_perm).
    Valid at small counts, where the χ² approximation is not.
    """
    labels = sorted(groups)
    if len(labels) < 2:
        raise ValueError("need at least two groups")
    obs = [groups[g] for g in labels]
    if any(k < 0 or k > n for k, n in obs):
        raise ValueError("need 0 <= successes <= trials")
    stat = _pearson_chi2(obs)
    pool = [1] * sum(k for k, _ in obs) + [0] * sum(n - k for k, n in obs)
    sizes = [n for _, n in obs]
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        perm, start = [], 0
        for n in sizes:
            perm.append((sum(pool[start : start + n]), n))
            start += n
        if _pearson_chi2(perm) >= stat - 1e-12:
            hits += 1
    return ProportionTest(chi2=stat, p_value=(1 + hits) / (1 + n_perm), n_perm=n_perm)


# --------------------------------------------------------------------------- §7 cost


class ModelPrice(BaseModel):
    """Price of one model on Alan AI, USD per million tokens (filled after the model probe)."""

    alias: str
    usd_per_mtok_in: float
    usd_per_mtok_out: float


class CostEstimate(BaseModel):
    runs: int
    per_model_usd: dict[str, float]
    total_usd: float
    with_margin_usd: float
    margin: float
    cap_usd: float
    within_cap: bool


def estimate_study_cost(
    n_tasks: int,
    repeats: int,
    prices: Sequence[ModelPrice],
    avg_tokens_in: float,
    avg_tokens_out: float,
    extra_runs: dict[str, int] | None = None,
    margin: float = 1.25,
    cap_usd: float = 8.0,
) -> CostEstimate:
    """Study cost (METHODS §7): runs × (tokens_in · price_in + tokens_out · price_out) / 1e6.

    ``avg_tokens_in`` / ``avg_tokens_out`` are per agent run (summed over all its steps).
    ``extra_runs`` adds runs for a model alias (e.g. sabotage runs on the cheapest model).
    ``within_cap`` compares the margin-inflated total with the cap.
    """
    if min(n_tasks, repeats) < 0 or min(avg_tokens_in, avg_tokens_out) < 0:
        raise ValueError("counts and tokens must be >= 0")
    extra = extra_runs or {}
    unknown = set(extra) - {p.alias for p in prices}
    if unknown:
        raise ValueError(f"extra_runs for unknown models: {sorted(unknown)}")
    per_model: dict[str, float] = {}
    runs = 0
    for p in prices:
        r = n_tasks * repeats + extra.get(p.alias, 0)
        runs += r
        per_run = (avg_tokens_in * p.usd_per_mtok_in + avg_tokens_out * p.usd_per_mtok_out) / 1e6
        per_model[p.alias] = r * per_run
    total = sum(per_model.values())
    return CostEstimate(
        runs=runs,
        per_model_usd=per_model,
        total_usd=total,
        with_margin_usd=total * margin,
        margin=margin,
        cap_usd=cap_usd,
        within_cap=total * margin <= cap_usd,
    )
