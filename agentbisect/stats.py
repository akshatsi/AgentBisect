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

from functools import lru_cache

from agentbisect.models import OraclePolicy, Verdict

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
