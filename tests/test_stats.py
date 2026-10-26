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
