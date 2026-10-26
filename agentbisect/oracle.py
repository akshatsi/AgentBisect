"""The oracle: restore a snapshot into a scratch worktree and run the check command on it.

The verdict comes only from exit codes. A timeout, a missing command, or a pytest exit code of 2-5
(interrupted, internal error, usage error, no tests collected) is ``Verdict.ERROR``, never PASS or
FAIL. How many times to run per snapshot, and when to stop, comes from ``stats`` via an
``OraclePolicy``; this module hard-codes none of it.
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

#: pytest exit codes that mean "the check could not give an answer" (pytest docs: 2 interrupted,
#: 3 internal error, 4 usage error, 5 no tests collected). 126/127: command not executable/found.
ERROR_EXIT_CODES = frozenset({2, 3, 4, 5, 126, 127})

Decider = Callable[[OraclePolicy, int, int], Verdict | None]


def fallback_policy() -> OraclePolicy:
    """Single run, no flake allowance. Used only if ``agentbisect.stats`` is unavailable."""
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
    """Evaluates snapshots of one run with one check command. Results are cached per SHA."""

    def __init__(
        self,
        run_dir: Path,
        config: OracleConfig,
        policy: OraclePolicy | None = None,
        decide: Decider | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.config = config
        self.policy = policy or fallback_policy()
        self._decide: Decider = decide or fallback_decide
        self.scratch = self.run_dir / "scratch"
        self._cache: dict[str, OracleResult] = {}
        self._restored: str | None = None

    def set_policy(self, policy: OraclePolicy, decide: Decider | None = None) -> None:
        """Change the policy. Cached results made under the old policy are dropped."""
        self.policy = policy
        if decide is not None:
            self._decide = decide
        self._cache.clear()

    def cached(self, sha: str) -> OracleResult | None:
        return self._cache.get(sha)

    def _restore(self, sha: str) -> None:
        if self._restored != sha:
            snapshot.restore(self.run_dir, sha, self.scratch)
            self._restored = sha

    def run_once(self, sha: str) -> OracleRun:
        """One raw execution of the check on snapshot ``sha`` (no policy, no cache)."""
        self._restore(sha)
        return execute_check(self.config.check, self.scratch, self.config.timeout_s)

    def evaluate(self, sha: str, step_index: int) -> tuple[OracleResult, bool]:
        """Decide pass/fail/error for snapshot ``sha``.

        Returns ``(result, fresh)``; ``fresh`` is False when the result came from the cache
        (several steps can share one snapshot, and the oracle runs once per snapshot).
        """
        hit = self._cache.get(sha)
        if hit is not None:
            return hit, False
        runs: list[OracleRun] = []
        n_fail = 0
        verdict: Verdict
        while True:
            run = self.run_once(sha)
            runs.append(run)
            kind = classify(run)
            if kind is Verdict.ERROR:
                verdict = Verdict.ERROR
                break
            n_fail += kind is Verdict.FAIL
            decided = self._decide(self.policy, n_fail, len(runs))
            if decided is None and len(runs) >= self.policy.max_runs:
                decided = fallback_decide(self.policy, n_fail, len(runs))  # never loop forever
            if decided is not None:
                verdict = decided
                break
        result = OracleResult(step_index=step_index, snapshot=sha, verdict=verdict, runs=runs)
        self._cache[sha] = result
        return result, True
