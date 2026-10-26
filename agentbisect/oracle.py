"""The oracle: restore a snapshot into a scratch worktree and run the check command on it.

The verdict comes only from exit codes (classification table in METHODS.md §2.1). How many times to
run per snapshot, and when to stop, comes from ``stats`` via an ``OraclePolicy``; this module
hard-codes none of it. Every run on a snapshot is kept, so calibration, search and confirmation
runs all count toward that snapshot's decision.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from agentbisect import snapshot
from agentbisect.models import OracleConfig, OraclePolicy, OracleResult, OracleRun, Verdict

TAIL_CHARS = 2000

#: Exit codes that mean "the check could not give an answer" (METHODS.md §2.1): pytest 3 internal
#: error, 4 usage error, 5 no tests collected; 126/127 command not runnable. Exit 2 is a collection
#: error, which after a passing step 0 is a break the agent caused, so it counts as FAIL.
ERROR_EXIT_CODES = frozenset({3, 4, 5, 126, 127})

#: A snapshot with this many error runs before ``decide`` returns has verdict ERROR (METHODS.md §2.1).
ERROR_RUNS_LIMIT = 2

Decider = Callable[[OraclePolicy, int, int], Verdict | None]
Executor = Callable[[str], OracleRun]


def fallback_policy() -> OraclePolicy:
    """Single run, no flake allowance. Default until the caller installs a ``stats`` policy."""
    return OraclePolicy(
        flake_rate=0.0, max_runs=1, per_probe_error=0.0, rule="single run (deterministic fallback)"
    )


def fallback_decide(policy: OraclePolicy, n_fail: int, n_runs: int) -> Verdict | None:
    if n_runs < policy.max_runs:
        return None
    return Verdict.FAIL if 2 * n_fail > n_runs else Verdict.PASS


def classify(run: OracleRun) -> Verdict:
    """Verdict of one execution of the check command."""
    if run.timed_out or run.exit_code is None or run.exit_code in ERROR_EXIT_CODES:
        return Verdict.ERROR
    return Verdict.PASS if run.exit_code == 0 else Verdict.FAIL


def count_runs(runs: list[OracleRun]) -> tuple[int, int, int]:
    """``(n_fail, n_counted, n_error)``. Error runs are not counted (METHODS.md §2.1)."""
    kinds = [classify(r) for r in runs]
    n_fail = kinds.count(Verdict.FAIL)
    n_error = kinds.count(Verdict.ERROR)
    return n_fail, len(kinds) - n_error, n_error


def _tail(data: bytes | str | None) -> str:
    if data is None:
        return ""
    text = data if isinstance(data, str) else data.decode(errors="replace")
    return text[-TAIL_CHARS:]


def execute_check(check: str, cwd: Path, timeout_s: float) -> OracleRun:
    """Run ``check`` once with ``shell=True`` in ``cwd``. The current Python's bin dir leads PATH."""
    env = dict(os.environ)
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Keep pytest's rootdir at the scratch worktree even when runs/ sits inside another project.
    env["PYTEST_ADDOPTS"] = f"--rootdir={shlex.quote(str(cwd))} " + env.get("PYTEST_ADDOPTS", "")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            check,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        return OracleRun(
            exit_code=None,
            duration_s=time.monotonic() - start,
            output_tail=_tail(exc.stdout),
            timed_out=True,
        )
    return OracleRun(
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        output_tail=_tail(proc.stdout),
    )


class Oracle:
    """Runs one check command on snapshots of one run.

    ``runs_for(sha)`` holds every run ever made on a snapshot. ``evaluate`` turns those runs into a
    verdict under the current policy, running more only if the policy asks for it. ``executor``
    replaces the real check (``sha -> OracleRun``); tests use it to simulate oracles quickly.
    """

    def __init__(
        self,
        run_dir: Path,
        config: OracleConfig,
        policy: OraclePolicy | None = None,
        decide: Decider | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.config = config
        self.policy = policy or fallback_policy()
        self._decide: Decider = decide or fallback_decide
        self._executor = executor
        self.scratch = self.run_dir / "scratch"
        self._runs: dict[str, list[OracleRun]] = {}
        self._cache: dict[str, OracleResult] = {}
        self._restored: str | None = None

    def set_policy(self, policy: OraclePolicy, decide: Decider | None = None) -> None:
        """Change the policy. Recorded runs stay; decisions made under the old policy are dropped."""
        self.policy = policy
        if decide is not None:
            self._decide = decide
        self._cache.clear()

    def runs_for(self, sha: str) -> list[OracleRun]:
        return self._runs.setdefault(sha, [])

    def _restore(self, sha: str) -> None:
        if self._restored != sha:
            snapshot.restore(self.run_dir, sha, self.scratch)
            self._restored = sha

    def run_once(self, sha: str) -> OracleRun:
        """One more execution of the check on snapshot ``sha``. The run is recorded."""
        if self._executor is not None:
            run = self._executor(sha)
        else:
            self._restore(sha)
            run = execute_check(self.config.check, self.scratch, self.config.timeout_s)
        self.runs_for(sha).append(run)
        return run

    def cached(self, sha: str) -> OracleResult | None:
        return self._cache.get(sha)

    def evaluate(self, sha: str, step_index: int) -> tuple[OracleResult, bool]:
        """Decide pass/fail/error for snapshot ``sha`` under the current policy.

        Existing runs count first; more are made only while the decision rule asks for them.
        Returns ``(result, fresh)``; ``fresh`` is False when the decision came from the cache.
        """
        hit = self._cache.get(sha)
        if hit is not None:
            return hit, False
        runs = self.runs_for(sha)
        verdict: Verdict
        while True:
            n_fail, n_counted, n_error = count_runs(runs)
            decided = self._decide(self.policy, n_fail, n_counted) if n_counted else None
            if decided is None and n_counted >= self.policy.max_runs:
                decided = fallback_decide(self.policy, n_fail, n_counted)  # never loop forever
            if decided is not None:
                verdict = decided
                break
            if n_error >= ERROR_RUNS_LIMIT:
                verdict = Verdict.ERROR
                break
            self.run_once(sha)
        result = OracleResult(step_index=step_index, snapshot=sha, verdict=verdict, runs=list(runs))
        self._cache[sha] = result
        return result, True

    def snapshot_result(self, sha: str, step_index: int, verdict: Verdict) -> OracleResult:
        """An ``OracleResult`` carrying every run recorded so far for ``sha``."""
        return OracleResult(
            step_index=step_index, snapshot=sha, verdict=verdict, runs=list(self.runs_for(sha))
        )
