"""Tests for agentbisect.stats against hand-computed values (derivations in docs/METHODS.md)."""

from __future__ import annotations

import math
import random

import pytest

from agentbisect import stats
from agentbisect.models import Verdict
from agentbisect.stats import (
    CALIBRATION_RUNS,
    CALIBRATION_RUNS_FLAKY,
    CONFIRM_RUNS,
    MAX_BISECTABLE_FLAKE,
    bisect_confidence,
    decide,
    flake_rate_from_calibration,
    make_policy,
)

# --------------------------------------------------------------------------- §2 policy


def test_ceil_log2():
    assert [stats.ceil_log2(m) for m in (1, 2, 3, 4, 5, 16, 17, 30, 32)] == [
        0,
        1,
        2,
        2,
        3,
        4,
        5,
        5,
        5,
    ]


def test_flake_rate_from_calibration():
    assert flake_rate_from_calibration(0, 6) == 0.0
    assert flake_rate_from_calibration(1, 20) == pytest.approx(2 / 22)
    assert flake_rate_from_calibration(3, 20) == pytest.approx(4 / 22)
    assert flake_rate_from_calibration(20, 20) == stats.FLAKE_RATE_CEILING
    assert flake_rate_from_calibration(5, 6) < 0.5
    with pytest.raises(ValueError):
        flake_rate_from_calibration(3, 2)


def test_sprt_untruncated_error():
    # 1 / (1 + 9^3) = 1/730
    assert stats.sprt_untruncated_error(0.1, 3) == pytest.approx(1 / 730)
    assert stats.sprt_untruncated_error(0.0, 1) == 0.0


def test_sprt_error_hand_computed():
    # d=1, M=1: a single run is wrong with probability p.
    assert stats.sprt_error(0.1, 1, 1) == pytest.approx(0.1)
    # d=2, M=3, p=0.1: two flips (0.01), or one flip in two runs (0.18) then a flip (0.1).
    assert stats.sprt_error(0.1, 2, 3) == pytest.approx(0.01 + 0.18 * 0.1)
    # d=2, M=3 expected runs: 2 runs always, a third with prob 0.18.
    assert stats.sprt_expected_runs(0.1, 2, 3) == pytest.approx(2.18)


def test_sprt_error_converges_to_untruncated():
    assert stats.sprt_error(0.2, 4, 199) == pytest.approx(
        stats.sprt_untruncated_error(0.2, 4), rel=1e-6
    )


@pytest.mark.parametrize(
    ("p", "d", "max_runs"),
    [(0.0, 1, 1), (0.05, 2, 5), (0.1, 3, 7), (0.2, 4, 15), (0.3, 6, 47)],
)
def test_policy_worked_numbers_n30(p, d, max_runs):
    """The table in METHODS.md §2.4."""
    pol = make_policy(p, 30)
    assert pol.per_probe_error == pytest.approx(0.05 / 7)
    assert pol.max_runs == max_runs
    assert stats._threshold(pol) == d
    if p > 0:
        assert stats.sprt_error(p, d, max_runs) <= pol.per_probe_error
        # d is minimal: d - 1 cannot meet the budget even without a cap.
        assert stats.sprt_untruncated_error(p, d - 1) > pol.per_probe_error


def test_make_policy_n_decisions_override():
    pol = make_policy(0.1, 30, n_decisions=32)
    assert pol.per_probe_error == pytest.approx(0.05 / 32)


def test_decide_deterministic():
    pol = make_policy(0.0, 30)
    assert pol.max_runs == 1
    assert decide(pol, 0, 0) is None
    assert decide(pol, 1, 1) == Verdict.FAIL
    assert decide(pol, 0, 1) == Verdict.PASS
    assert decide(pol, 0, 3) == Verdict.PASS  # calibration runs at step 0
    assert decide(pol, 1, 2) == Verdict.FAIL  # tie past the cap


def test_decide_sequential():
    pol = make_policy(0.1, 30)  # d = 3, cap 7
    assert decide(pol, 1, 1) is None
    assert decide(pol, 2, 2) is None
    assert decide(pol, 3, 3) == Verdict.FAIL
    assert decide(pol, 0, 3) == Verdict.PASS
    assert decide(pol, 3, 4) is None  # 3 fail, 1 pass: diff 2
    assert decide(pol, 4, 5) == Verdict.FAIL  # diff 3
    assert decide(pol, 3, 6) is None
    assert decide(pol, 4, 7) == Verdict.FAIL  # cap: majority
    assert decide(pol, 3, 7) == Verdict.PASS


@pytest.mark.parametrize("p", [0.0, 0.05, 0.1, 0.2, 0.3])
def test_decide_always_decides_at_cap(p):
    pol = make_policy(p, 30)
    for n_fail in range(pol.max_runs + 1):
        assert decide(pol, n_fail, pol.max_runs) is not None


def test_bisect_confidence():
    pol = make_policy(0.1, 30)
    e = stats.sprt_error(0.1, 3, 7)
    assert bisect_confidence(pol, 7) == pytest.approx(1 - 7 * e)
    assert bisect_confidence(pol, 7) >= 0.95
    assert bisect_confidence(make_policy(0.0, 30), 7) == pytest.approx(
        1 - stats.DETERMINISTIC_RESIDUAL
    )


def test_deterministic_residual():
    # max_p (1-p)^6 p^3 at p = 1/3: (2/3)^6 (1/3)^3 = 64 / 19683
    assert math.isclose(stats.DETERMINISTIC_RESIDUAL, 64 / 19683)


# --------------------------------------------------------------------------- §2 Monte Carlo


class _Sim:
    """Simulates the full METHODS §2.6 procedure against a monotone flaky oracle."""

    def __init__(self, m: int, t_err: int, p: float, rng: random.Random):
        self.m, self.t_err, self.p, self.rng = m, t_err, p, rng
        self.fails = [0] * (m + 1)
        self.runs = [0] * (m + 1)
        self.probes = 0

    def run(self, i: int) -> bool:
        truly_fail = i >= self.t_err
        fail = truly_fail != (self.rng.random() < self.p)
        self.runs[i] += 1
        self.fails[i] += fail
        return fail

    def decide(self, policy, i: int) -> Verdict:
        while (v := decide(policy, self.fails[i], self.runs[i])) is None:
            self.run(i)
        return v

    def bisect(self, policy) -> tuple[int, int]:
        lo, hi = 0, self.m
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.runs[mid] == 0:
                self.run(mid)
            self.probes += 1
            if self.decide(policy, mid) == Verdict.FAIL:
                hi = mid
            else:
                lo = mid
        return lo, hi

    def unexpected_at_endpoints(self) -> tuple[int, int]:
        k = self.fails[0] + (self.runs[self.m] - self.fails[self.m])
        return k, self.runs[0] + self.runs[self.m]

    def procedure(self) -> int | None:
        m = self.m
        for _ in range(CALIBRATION_RUNS):
            self.run(0)
            self.run(m)
        if self.fails[0] == self.runs[0] or self.fails[m] == 0:
            return None  # precondition fails outright
        k, n = self.unexpected_at_endpoints()
        k_extra = n_extra = 0
        if k == 0:
            lo, hi = self.bisect(make_policy(0.0, m))
            conf_lo = [self.run(lo) for _ in range(CONFIRM_RUNS)]
            conf_hi = [self.run(hi) for _ in range(CONFIRM_RUNS)]
            if not any(conf_lo) and all(conf_hi):
                return hi
            # Escalate: count confirmation disagreements away from the endpoints.
            for idx, conf, bad in ((lo, conf_lo, True), (hi, conf_hi, False)):
                if idx not in (0, m):
                    k_extra += sum(1 for f in conf if f == bad)
                    n_extra += len(conf)
        while self.runs[0] < CALIBRATION_RUNS_FLAKY:
            self.run(0)
        while self.runs[m] < CALIBRATION_RUNS_FLAKY:
            self.run(m)
        k, n = self.unexpected_at_endpoints()
        p_hat = flake_rate_from_calibration(k + k_extra, n + n_extra)
        if p_hat > MAX_BISECTABLE_FLAKE:
            return None
        policy = make_policy(p_hat, m)
        if self.decide(policy, 0) != Verdict.PASS or self.decide(policy, m) != Verdict.FAIL:
            return None
        return self.bisect(policy)[1]


@pytest.mark.parametrize(("p", "max_refused"), [(0.1, 0.01), (0.2, 0.06)])
def test_monte_carlo_flaky_bisect_error_rate(p, max_refused):
    """Seeded simulation: P(wrong step) <= TARGET_ERROR for n = 30 under flip rate p."""
    rng = random.Random(20261026)
    trials, wrong, refused = 3000, 0, 0
    for _ in range(trials):
        t_err = rng.randint(1, 30)
        got = _Sim(30, t_err, p, rng).procedure()
        if got is None:
            refused += 1
        elif got != t_err:
            wrong += 1
    assert wrong / trials <= stats.TARGET_ERROR
    # Refusals (false "not bisectable") stay rare, too.
    assert refused / trials <= max_refused


def test_monte_carlo_deterministic_uses_one_run_per_probe():
    rng = random.Random(1)
    for t_err in (1, 15, 30):
        sim = _Sim(30, t_err, 0.0, rng)
        assert sim.procedure() == t_err
        assert sim.probes <= stats.ceil_log2(30)
        probed = [i for i in range(1, 30) if sim.runs[i]]
        # Each probed snapshot: one bisect run, plus CONFIRM_RUNS at the two boundary ones.
        assert sum(sim.runs[i] for i in probed) <= len(probed) + 2 * CONFIRM_RUNS


def test_monte_carlo_p01_single_trial_seeded():
    """The M1 acceptance case: p = 0.1, seeded, right step for k in {1, 15, 30}."""
    rng = random.Random(7)
    for t_err in (1, 15, 30):
        assert _Sim(30, t_err, 0.1, rng).procedure() == t_err


def test_math_import_used():
    assert math.isclose(stats.sprt_untruncated_error(0.25, 1), 0.25)


# --------------------------------------------------------------------------- §6 intervals


def test_wilson_interval_hand_computed():
    # 8/10: center 0.99208/1.38416, half 1.96*sqrt(0.025604)/1.38416
    lo, hi = stats.wilson_interval(8, 10)
    assert lo == pytest.approx(0.490157, abs=1e-5)
    assert hi == pytest.approx(0.943318, abs=1e-5)
    lo, hi = stats.wilson_interval(0, 10)
    assert lo == 0.0
    assert hi == pytest.approx(3.8416 / 13.8416, abs=1e-9)  # z^2 / (n + z^2)
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = stats.wilson_interval(10, 10)
    assert hi == 1.0
    assert lo == pytest.approx(10 / 13.8416, abs=1e-9)


def test_bootstrap_ci_seeded_and_sane():
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    def mean(xs):
        return sum(xs) / len(xs)

    a = stats.bootstrap_ci(data, mean, seed=3)
    b = stats.bootstrap_ci(data, mean, seed=3)
    assert a == b
    assert a[0] < 5.5 < a[1]
    assert a[0] > 3.0 and a[1] < 8.0
    assert stats.bootstrap_ci([2.0] * 5, mean) == (2.0, 2.0)


# --------------------------------------------------------------------------- §5 Kaplan–Meier

DUR = [1, 2, 2, 3, 4, 5]
OBS = [True, True, False, True, False, True]


def test_kaplan_meier_hand_computed():
    curve = stats.kaplan_meier(DUR, OBS)
    assert [pt.time for pt in curve] == [1, 2, 3, 4, 5]
    assert [pt.at_risk for pt in curve] == [6, 5, 3, 2, 1]
    assert [pt.events for pt in curve] == [1, 1, 1, 0, 1]
    assert [pt.censored for pt in curve] == [0, 1, 0, 1, 0]
    s = [pt.survival for pt in curve]
    assert s == pytest.approx([5 / 6, 4 / 6, 4 / 9, 4 / 9, 0.0])
    # Greenwood: 1/30, + 1/20, + 1/6
    assert curve[2].greenwood == pytest.approx(0.25)
    # log-log band at t=3: se = sqrt(.25)/|ln(4/9)|, bounds (4/9)^exp(±1.96 se)
    assert curve[2].lower == pytest.approx(0.066183, abs=1e-5)
    assert curve[2].upper == pytest.approx(0.784913, abs=1e-5)
    assert curve[1].lower == pytest.approx(0.194608, abs=1e-5)
    assert curve[1].upper == pytest.approx(0.904436, abs=1e-5)
    assert curve[0].lower == pytest.approx(0.273110, abs=1e-5)
    # S = 0 at t=5: lower 0, upper carried forward
    assert curve[4].lower == 0.0
    assert curve[4].upper == curve[3].upper


def test_km_median_ci_hand_computed():
    res = stats.km_median_ci(DUR, OBS)
    assert res.median == 3
    assert res.lower == 1  # lower band is 0.273 <= 0.5 already at t=1
    assert res.upper is None  # upper band never reaches 0.5: "not reached"
    assert (res.n, res.events) == (6, 4)


def test_km_all_censored_and_no_censoring():
    res = stats.km_median_ci([3, 4, 5], [False, False, False])
    assert res.median is None and res.lower is None and res.upper is None
    curve = stats.kaplan_meier([1, 2, 3, 4], [True] * 4)
    assert [pt.survival for pt in curve] == pytest.approx([0.75, 0.5, 0.25, 0.0])
    assert stats.km_median_ci([1, 2, 3, 4], [True] * 4).median == 2


def test_chi2_sf():
    assert stats.chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-6)
    assert stats.chi2_sf(5.0, 2) == pytest.approx(math.exp(-2.5))
    assert stats.chi2_sf(7.814728, 3) == pytest.approx(0.05, abs=1e-6)
    assert stats.chi2_sf(0.0, 2) == 1.0


def test_logrank_two_groups_hand_computed():
    # A dies at 1, 2; B at 3, 4. O-E for A: 0.5 + 2/3; V: 0.25 + 2/9.
    res = stats.logrank_test({"A": ([1, 2], [True, True]), "B": ([3, 4], [True, True])})
    u, v = 0.5 + 2 / 3, 0.25 + 2 / 9
    assert res.chi2 == pytest.approx(u * u / v)
    assert res.df == 1
    assert res.p_value == pytest.approx(stats.chi2_sf(u * u / v, 1))


def test_logrank_identical_groups_is_zero():
    g = ([1, 2, 3], [True, True, False])
    res = stats.logrank_test({"a": g, "b": g, "c": g})
    assert res.chi2 == pytest.approx(0.0, abs=1e-12)
    assert res.df == 2
    assert res.p_value == pytest.approx(1.0)


# --------------------------------------------------------------------------- §7 cost


def test_estimate_study_cost_hand_computed():
    prices = [
        stats.ModelPrice(alias="a", usd_per_mtok_in=0.1, usd_per_mtok_out=0.4),
        stats.ModelPrice(alias="b", usd_per_mtok_in=1.0, usd_per_mtok_out=3.0),
    ]
    est = stats.estimate_study_cost(10, 1, prices, 200_000, 10_000, extra_runs={"a": 9})
    # a: 19 runs x (0.02 + 0.004); b: 10 runs x (0.2 + 0.03)
    assert est.runs == 29
    assert est.per_model_usd["a"] == pytest.approx(0.456)
    assert est.per_model_usd["b"] == pytest.approx(2.3)
    assert est.total_usd == pytest.approx(2.756)
    assert est.with_margin_usd == pytest.approx(3.445)
    assert est.within_cap
    with pytest.raises(ValueError):
        stats.estimate_study_cost(1, 1, prices, 1, 1, extra_runs={"zzz": 1})


def test_proportion_test():
    # 2x2 table 8/10 vs 2/10: Pearson chi2 = 20 * (8*8 - 2*2)^2 / (10*10*10*10) = 7.2
    res = stats.proportion_test({"a": (8, 10), "b": (2, 10)}, n_perm=2000, seed=1)
    assert res.chi2 == pytest.approx(7.2)
    # exact Fisher two-sided p for this table is 0.023; the permutation p must be close.
    assert 0.01 < res.p_value < 0.04
    same = stats.proportion_test({"a": (5, 10), "b": (5, 10), "c": (5, 10)}, n_perm=200)
    assert same.chi2 == 0.0
    assert same.p_value == 1.0
    assert stats.proportion_test({"a": (3, 5), "b": (1, 5)}, seed=4) == stats.proportion_test(
        {"a": (3, 5), "b": (1, 5)}, seed=4
    )
