"""Bisect a trace for the step after which the oracle fails for good.

Implements the procedure in docs/METHODS.md §2.6: calibrate the oracle at the two endpoints,
binary-search the *candidate* steps (those that change the repo), confirm the boundary, and check
for a non-monotone history. All statistical numbers come from ``agentbisect.stats``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from pathlib import Path

from agentbisect import snapshot, stats
from agentbisect.analyze import analyze
from agentbisect.models import (
    BisectResult,
    BisectStatus,
    CulpritReport,
    OracleConfig,
    OraclePolicy,
    OracleResult,
    OracleRun,
    RunReport,
    Step,
    Trace,
    Verdict,
)
from agentbisect.oracle import (
    ERROR_EXIT_CODES,
    Executor,
    Oracle,
    classify,
    count_runs,
    describe_error,
    run_command,
    venv_problem,
)


def describe_step(step: Step) -> str:
    """Short label such as ``edit calc/__init__.py`` or ``shell: pytest -q``."""
    args = step.args or {}
    target = args.get("path") or args.get("file") or args.get("file_path")
    if target:
        return f"{step.kind.value} {target}"
    cmd = args.get("command") or args.get("cmd")
    if cmd:
        text = str(cmd).replace("\n", " ")
        return f"{step.kind.value}: {text[:60]}" + ("..." if len(text) > 60 else "")
    return f"{step.kind.value} ({step.tool})"


class _Stop(Exception):
    """Internal: the history cannot be bisected. Carries the reason."""


class _Run:
    """State of one bisect: the trace, its candidates, the oracle, and every decision made."""

    def __init__(
        self, trace: Trace, run_dir: Path, config: OracleConfig, executor: Executor | None
    ) -> None:
        self.trace = trace
        self.n = trace.n
        self.config = config
        trees = snapshot.tree_map(run_dir)
        key = [trees.get(trace.snapshot_at(i), trace.snapshot_at(i)) for i in range(self.n + 1)]
        # Candidates c_0 = 0 < c_1 < ...: steps whose state differs from the step before (§1.1).
        self.cand = [0] + [i for i in range(1, self.n + 1) if key[i] != key[i - 1]]
        self.m = len(self.cand) - 1
        self.keys = [key[c] for c in self.cand]
        canon: dict[str, str] = {}
        self.sha = [
            canon.setdefault(self.keys[j], trace.snapshot_at(c)) for j, c in enumerate(self.cand)
        ]
        self.oracle = Oracle(run_dir, config, executor=executor, python=trace.meta.python)
        self.order: list[int] = []  # candidate positions in the order first decided
        self.verdicts: dict[int, Verdict] = {}

    # -- decisions ---------------------------------------------------------

    def set_policy(self, policy: OraclePolicy) -> None:
        self.oracle.set_policy(policy, stats.decide)

    def decide(self, j: int) -> Verdict:
        """Oracle decision for candidate ``j`` under the current policy."""
        result, _ = self.oracle.evaluate(self.sha[j], self.cand[j])
        if j not in self.verdicts:
            self.order.append(j)
        self.verdicts[j] = result.verdict
        return result.verdict

    def probes(self) -> list[OracleResult]:
        return [
            self.oracle.snapshot_result(self.sha[j], self.cand[j], self.verdicts[j])
            for j in self.order
        ]

    def error_cause(self, sha: str) -> str:
        """Why the last erroring run on ``sha`` gave no answer (or a generic phrase)."""
        errors = [r for r in self.oracle.runs_for(sha) if classify(r) is Verdict.ERROR]
        if not errors:
            return "the oracle gave no answer"
        return describe_error(errors[-1], self.config.timeout_s)

    def counts(self, sha: str) -> tuple[int, int]:
        n_fail, n_counted, _ = count_runs(self.oracle.runs_for(sha))
        return n_fail, n_counted

    # -- stage 1 / 4: calibration -----------------------------------------

    def run_to(self, sha: str, total: int) -> None:
        runs = self.oracle.runs_for(sha)
        while len(runs) < total:
            self.oracle.run_once(sha)

    def endpoint_check(self) -> None:
        """Stop if either endpoint cannot be tested or has the wrong unanimous result."""
        for j, name, wrong in (
            (0, "step 0", Verdict.FAIL),
            (self.m, f"step {self.n}", Verdict.PASS),
        ):
            runs = self.oracle.runs_for(self.sha[j])
            _, n_counted, _ = count_runs(runs)
            if n_counted == 0:
                raise _Stop(f"oracle error at {name}: {self.error_cause(self.sha[j])}")
            kinds = {classify(r) for r in runs} - {Verdict.ERROR}
            if kinds == {wrong}:
                if j == 0:
                    raise _Stop("oracle does not pass at step 0")
                raise _Stop(f"oracle does not fail at the final step {self.n}")

    def unexpected(self) -> tuple[int, int]:
        """``(unexpected, runs)`` over every counted run at the two endpoints."""
        f0, n0 = self.counts(self.sha[0])
        fn, nn = self.counts(self.sha[self.m])
        return f0 + (nn - fn), n0 + nn

    # -- search -------------------------------------------------------------

    def search(self) -> tuple[int, int]:
        """Binary search with ``git bisect skip`` for ERROR candidates (§1.2, §2.1)."""
        lo, hi = 0, self.m
        while hi - lo > 1:
            mid = (lo + hi) // 2
            tried = [mid]
            for off in range(1, hi - lo):
                tried += [c for c in (mid + off, mid - off) if lo < c < hi]
            for cand in tried:
                verdict = self.decide(cand)
                if verdict is not Verdict.ERROR:
                    break
            else:
                raise _Stop(
                    f"steps {self.cand[lo + 1]}..{self.cand[hi]} could not be tested "
                    f"(oracle errors: {self.error_cause(self.sha[hi - 1])})"
                )
            if verdict is Verdict.PASS:
                lo = cand
            else:
                hi = cand
        return lo, hi

    # -- confirmation (§2.5) ----------------------------------------------

    def confirm(self, lo: int, hi: int) -> tuple[bool, int, int]:
        """Add CONFIRM_RUNS fresh runs at both boundary snapshots.

        Returns ``(agreed, unexpected_at_non_endpoint_boundaries, runs_there)``.
        """
        extra_k = extra_n = 0
        agreed = True
        for j, want in ((lo, Verdict.PASS), (hi, Verdict.FAIL)):
            sha = self.sha[j]
            fresh = [self.oracle.run_once(sha) for _ in range(stats.CONFIRM_RUNS)]
            kinds = [classify(r) for r in fresh if classify(r) is not Verdict.ERROR]
            bad = sum(k is not want for k in kinds)
            agreed = agreed and bad == 0 and len(kinds) > 0
            if j not in (0, self.m):
                extra_k += bad
                extra_n += len(kinds)
        return agreed, extra_k, extra_n

    # -- non-monotone check (§3) ------------------------------------------

    def looks_non_monotone(self, lo: int, hi: int) -> bool:
        left = set(self.keys[: lo + 1])
        if any(k in left for k in self.keys[hi:]):  # T1: a tree repeats across the boundary
            return True
        seen_fail_at: int | None = None  # T2: a FAIL left of a PASS
        for j in sorted(self.verdicts):
            if self.verdicts[j] is Verdict.FAIL and seen_fail_at is None:
                seen_fail_at = j
            elif self.verdicts[j] is Verdict.PASS and seen_fail_at is not None:
                return True
        return False

    def linear_scan(self) -> list[int]:
        """Decide every candidate and return all pass->fail transitions as step indices."""
        p = self.oracle.policy.flake_rate
        self.set_policy(stats.make_policy(p, self.m, n_decisions=self.m + 1))
        by_key: dict[str, Verdict] = {}
        known: list[tuple[int, Verdict]] = []
        for j in range(self.m + 1):
            if self.keys[j] not in by_key:
                by_key[self.keys[j]] = self.decide(j)
            else:
                self.verdicts.setdefault(j, by_key[self.keys[j]])
            if by_key[self.keys[j]] is not Verdict.ERROR:
                known.append((j, by_key[self.keys[j]]))
        transitions = [
            self.cand[b]
            for (_, va), (b, vb) in zip(known, known[1:], strict=False)
            if va is Verdict.PASS and vb is Verdict.FAIL
        ]
        return transitions


def _not_bisectable(
    config: OracleConfig, reason: str, run: _Run | None, policy: OraclePolicy | None, n_pre: int
) -> BisectResult:
    return BisectResult(
        check=config.check,
        status=BisectStatus.NOT_BISECTABLE,
        reason=reason,
        probes=run.probes() if run else [],
        precondition_probes=n_pre,
        policy=policy,
    )


def bisect(
    trace: Trace, run_dir: Path, config: OracleConfig, executor: Executor | None = None
) -> BisectResult:
    """Find the decisive step ``t_err`` for ``config.check`` on ``trace``.

    ``executor`` (``sha -> OracleRun``) replaces the real check; tests use it to simulate oracles.
    The check runs with ``trace.meta.python`` when set (the project's own venv).
    """
    run_dir = Path(run_dir).resolve()
    if trace.n == 0:
        return _not_bisectable(config, "trace has no steps", None, None, 0)
    problem = None if executor else venv_problem(trace.meta.python)
    if problem:
        return _not_bisectable(config, problem, None, None, 0)
    r = _Run(trace, run_dir, config, executor)
    if r.m == 0 or r.keys[0] == r.keys[r.m]:
        return _not_bisectable(
            config, "the repo at the final step is identical to step 0", None, None, 0
        )
    try:
        return _procedure(r)
    except _Stop as stop:
        n_pre = sum(1 for j in r.order if j in (0, r.m))
        return _not_bisectable(config, str(stop), r, r.oracle.policy, n_pre)


def _procedure(r: _Run) -> BisectResult:
    m = r.m
    base, last = r.sha[0], r.sha[m]

    # Stage 1: CALIBRATION_RUNS runs at each endpoint.
    r.run_to(base, stats.CALIBRATION_RUNS)
    r.run_to(last, stats.CALIBRATION_RUNS)
    r.endpoint_check()
    k, _ = r.unexpected()
    k_extra = n_extra = 0

    if k == 0:  # deterministic path: assume p = 0, confirm the boundary afterwards
        policy = stats.make_policy(0.0, m)
        r.set_policy(policy)
        _decide_endpoints(r)
        lo, hi = r.search()
        agreed, k_extra, n_extra = r.confirm(lo, hi)
        if agreed:
            return _finish(r, policy, lo, hi)

    # Stage 2 / flaky path.
    r.run_to(base, stats.CALIBRATION_RUNS_FLAKY)
    r.run_to(last, stats.CALIBRATION_RUNS_FLAKY)
    k, n = r.unexpected()
    p_hat = stats.flake_rate_from_calibration(k + k_extra, n + n_extra)
    if p_hat > stats.MAX_BISECTABLE_FLAKE:
        raise _Stop(f"oracle too flaky: estimated flip rate {p_hat:.2f}")
    policy = stats.make_policy(p_hat, m)
    r.set_policy(policy)
    _decide_endpoints(r)
    lo, hi = r.search()
    return _finish(r, policy, lo, hi)


def _decide_endpoints(r: _Run) -> None:
    v0 = r.decide(0)
    vn = r.decide(r.m)
    if v0 is Verdict.ERROR or vn is Verdict.ERROR:
        j, where = (0, "step 0") if v0 is Verdict.ERROR else (r.m, f"step {r.n}")
        raise _Stop(f"oracle error at {where}: {r.error_cause(r.sha[j])}")
    if v0 is not Verdict.PASS:
        raise _Stop("oracle does not pass at step 0")
    if vn is not Verdict.FAIL:
        raise _Stop(f"oracle does not fail at the final step {r.n}")


def _finish(r: _Run, policy: OraclePolicy, lo: int, hi: int) -> BisectResult:
    transitions = [r.cand[hi]]
    non_monotone = False
    if r.looks_non_monotone(lo, hi):
        found = r.linear_scan()
        if not found:
            raise _Stop("linear scan found no pass->fail transition (oracle too unstable)")
        transitions = found
        non_monotone = len(found) > 1
        policy = r.oracle.policy
    probes = r.probes()
    return BisectResult(
        check=r.config.check,
        status=BisectStatus.FOUND,
        decisive_step=transitions[-1],
        probes=probes,
        precondition_probes=2,
        policy=policy,
        non_monotone=non_monotone,
        transitions=transitions,
        confidence=stats.bisect_confidence(policy, len(probes)),
    )


# --------------------------------------------------------------------------- run: one entry point

#: The auto-oracle runs the whole suite with this command (``-rA`` lists every test's outcome, and
#: ``--continue-on-collection-errors`` keeps one broken file from hiding the other tests).
SUITE_CMD = "python -m pytest -q"
_SUITE_FLAGS = "-rA --continue-on-collection-errors"

#: The auto-oracle bisects at most this many newly failing tests (sorted by node ID); the rest are
#: listed in ``RunReport.checks`` with ``CAP_NOTE`` and no culprit.
MAX_AUTO_CULPRITS = 10
CAP_NOTE = "  (not bisected: cap)"

SuiteRunner = Callable[[str], tuple[OracleRun, str]]
ExecutorFactory = Callable[[str], Executor]

_OUTCOME_LINE = re.compile(r"^(PASSED|FAILED|ERROR) (\S.*)$")


def parse_outcomes(output: str) -> dict[str, str]:
    """Map pytest node ID -> ``PASSED``/``FAILED``/``ERROR`` from a ``-rA`` summary.

    A collection error names a file, not a test, so its key has no ``::``.
    """
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        m = _OUTCOME_LINE.match(line.rstrip())
        if m:
            status, rest = m.groups()
            outcomes[rest.split(" - ", 1)[0].strip()] = status
    return outcomes


def newly_failing(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Tests that pass in ``before`` and fail (or error) in ``after``, in the order of ``before``.

    A file that no longer collects takes all its passing tests with it. A test missing from
    ``after`` (deleted or renamed) is not a failure.
    """
    broken_files = {k for k, v in after.items() if v != "PASSED" and "::" not in k}
    out = []
    for node, status in before.items():
        if status != "PASSED":
            continue
        now = after.get(node)
        if (now is not None and now != "PASSED") or (
            now is None and node.split("::", 1)[0] in broken_files
        ):
            out.append(node)
    return out


def _suite_problem(run: OracleRun, where: str, timeout_s: float) -> str | None:
    if run.timed_out or run.exit_code is None or run.exit_code in ERROR_EXIT_CODES:
        return f"full suite could not run at {where}: {describe_error(run, timeout_s)}"
    return None


def suite_outcomes(
    run_dir: Path,
    sha: str,
    timeout_s: float,
    suite_runner: SuiteRunner | None = None,
    python: Path | None = None,
) -> tuple[dict[str, str] | None, str | None]:
    """Run the whole suite once on snapshot ``sha``: ``(node ID -> PASSED/FAILED/ERROR, problem)``.

    ``problem`` is a reason (and the outcomes are None) when the suite could not run. The study
    uses it to learn which tests pass at step 0. ``python`` is the project's interpreter.
    """
    run_dir = Path(run_dir).resolve()
    scratch = run_dir / "scratch"
    missing = None if suite_runner else venv_problem(python)
    if missing:
        return None, missing

    def default_runner(target: str) -> tuple[OracleRun, str]:
        snapshot.restore(run_dir, target, scratch)
        return run_command(f"{SUITE_CMD} {_SUITE_FLAGS}", scratch, timeout_s, python)

    run, out = (suite_runner or default_runner)(sha)
    problem = _suite_problem(run, "the base snapshot", timeout_s)
    if problem:
        return None, problem
    return parse_outcomes(out), None


def discover_failing_tests(
    trace: Trace, run_dir: Path, timeout_s: float, suite_runner: SuiteRunner | None = None
) -> tuple[list[str], str | None]:
    """Auto-oracle (ARCHITECTURE decision 3): run the suite at step 0 and step n.

    Returns ``(node IDs that pass at step 0 and fail at step n, problem)``. ``problem`` is a
    reason when the suite could not be run or had no passing test to start from.
    """
    run_dir = Path(run_dir).resolve()
    scratch = run_dir / "scratch"
    python = trace.meta.python
    missing = None if suite_runner else venv_problem(python)
    if missing:
        return [], missing

    def default_runner(sha: str) -> tuple[OracleRun, str]:
        snapshot.restore(run_dir, sha, scratch)
        return run_command(f"{SUITE_CMD} {_SUITE_FLAGS}", scratch, timeout_s, python)

    runner = suite_runner or default_runner
    run0, out0 = runner(trace.meta.base_snapshot)
    problem = _suite_problem(run0, "step 0", timeout_s)
    if problem:
        return [], problem
    before = parse_outcomes(out0)
    if "PASSED" not in before.values():
        return [], "no test passes at step 0, so no test can have been broken"
    run_n, out_n = runner(trace.snapshot_at(trace.n))
    problem = _suite_problem(run_n, f"the final step {trace.n}", timeout_s)
    if problem:
        return [], problem
    return newly_failing(before, parse_outcomes(out_n)), None


def culprit_report(trace: Trace, run_dir: Path, result: BisectResult) -> CulpritReport:
    """A ``CulpritReport`` for one bisect result: step, diff and detection lag if FOUND."""
    report = CulpritReport(run_id=trace.meta.run_id, check=result.check, bisect=result)
    if result.status is BisectStatus.FOUND and result.decisive_step is not None:
        t = result.decisive_step
        report.step = trace.steps[t - 1]
        report.diff = snapshot.diff(run_dir, trace.snapshot_at(t - 1), trace.snapshot_at(t))
        report.analysis = analyze(trace, result, run_dir)
    return report


def bisect_run(
    trace: Trace,
    run_dir: Path,
    check: str | None,
    timeout_s: float = 120.0,
    *,
    executor_factory: ExecutorFactory | None = None,
    suite_runner: SuiteRunner | None = None,
) -> RunReport:
    """Everything ``agentbisect run`` does, except printing and saving ``bisect.json``.

    With ``check``, bisect that one command. Without it (auto-oracle), run the full suite at step
    0 and step n and bisect every test that passes at 0 and fails at n, each with the check
    ``python -m pytest -q <node id>``. Each FOUND result gets its step, its diff and its detection
    lag (``analyze``). Culprits come back FOUND first, ordered by decisive step, then not
    bisectable ones. Auto-oracle bisects at most ``MAX_AUTO_CULPRITS`` tests (sorted by node ID);
    the others are named in ``checks`` with ``CAP_NOTE`` and get no culprit. A missing project
    venv (``trace.meta.python``) gives one not-bisectable culprit.

    An auto-oracle run with nothing newly failing returns no culprits. If the suite itself could
    not run, it returns one not-bisectable culprit whose check is ``SUITE_CMD`` and whose reason
    says why. Nothing is written here except the scratch worktree; the caller saves the report.

    ``executor_factory`` (``check -> Executor``) and ``suite_runner`` (``sha -> (run, output)``)
    replace the real subprocesses; tests use them. Real runs leave them None.
    """
    run_dir = Path(run_dir).resolve()
    auto = check is None
    checks: list[str]
    skipped: list[str] = []
    if check is not None:
        problem = None if executor_factory else venv_problem(trace.meta.python)
        if problem:
            failed = BisectResult(check=check, status=BisectStatus.NOT_BISECTABLE, reason=problem)
            return RunReport(
                run_id=trace.meta.run_id,
                checks=[check],
                culprits=[culprit_report(trace, run_dir, failed)],
            )
        checks = [check]
    else:
        nodes, problem = discover_failing_tests(trace, run_dir, timeout_s, suite_runner)
        if problem:
            failed = BisectResult(
                check=SUITE_CMD, status=BisectStatus.NOT_BISECTABLE, reason=problem
            )
            return RunReport(
                run_id=trace.meta.run_id,
                checks=[SUITE_CMD],
                culprits=[culprit_report(trace, run_dir, failed)],
                auto_oracle=True,
            )
        nodes = sorted(nodes)
        checks = [f"{SUITE_CMD} {shlex.quote(node)}" for node in nodes]
        skipped = [c + CAP_NOTE for c in checks[MAX_AUTO_CULPRITS:]]
        checks = checks[:MAX_AUTO_CULPRITS]

    culprits = []
    for one in checks:
        config = OracleConfig(check=one, timeout_s=timeout_s)
        executor = executor_factory(one) if executor_factory else None
        result = bisect(trace, run_dir, config, executor=executor)
        culprits.append(culprit_report(trace, run_dir, result))
    culprits.sort(
        key=lambda c: (
            c.bisect.status is not BisectStatus.FOUND,
            c.bisect.decisive_step or 0,
            c.check,
        )
    )
    if auto:
        checks = checks + skipped
    return RunReport(run_id=trace.meta.run_id, checks=checks, culprits=culprits, auto_oracle=auto)
