import sys
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.models import OracleConfig, OraclePolicy, OracleRun, Verdict
from agentbisect.oracle import Oracle, classify, describe_error, execute_check, run_command

PY = f'"{sys.executable}"'


def _run(code: int | None, timed_out: bool = False) -> OracleRun:
    return OracleRun(exit_code=code, duration_s=0.0, timed_out=timed_out)


@pytest.mark.parametrize(
    ("code", "timed_out", "expected"),
    [
        (0, False, Verdict.PASS),
        (1, False, Verdict.FAIL),
        (2, False, Verdict.FAIL),
        (3, False, Verdict.ERROR),
        (4, False, Verdict.ERROR),
        (5, False, Verdict.ERROR),
        (127, False, Verdict.ERROR),
        (None, True, Verdict.ERROR),
        (7, False, Verdict.FAIL),
    ],
)
def test_classify(code: int | None, timed_out: bool, expected: Verdict) -> None:
    assert classify(_run(code, timed_out)) is expected


@pytest.fixture
def run_dir(tmp_path: Path) -> tuple[Path, str, str]:
    work = tmp_path / "work"
    work.mkdir()
    (work / "flag.txt").write_text("ok\n")
    run = tmp_path / "run"
    good = snapshot.init_shadow(run, work)
    (work / "flag.txt").write_text("bad\n")
    bad, _ = snapshot.take_snapshot(run, work, "bad")
    return run, good, bad


CHECK = f"{PY} -c \"import sys; sys.exit(0 if open('flag.txt').read() == 'ok\\n' else 1)\""


def test_pass_and_fail_on_restored_snapshots(run_dir: tuple[Path, str, str]) -> None:
    run, good, bad = run_dir
    oracle = Oracle(run, OracleConfig(check=CHECK, timeout_s=30))
    r_good, fresh = oracle.evaluate(good, 0)
    assert (r_good.verdict, fresh) == (Verdict.PASS, True)
    r_bad, _ = oracle.evaluate(bad, 1)
    assert r_bad.verdict is Verdict.FAIL
    assert r_bad.runs[0].exit_code == 1
    assert r_bad.runs[0].duration_s >= 0
    # The scratch worktree lives under the run dir.
    assert (run / "scratch" / "flag.txt").exists()


def test_cache_is_per_snapshot(run_dir: tuple[Path, str, str]) -> None:
    run, good, _ = run_dir
    oracle = Oracle(run, OracleConfig(check=CHECK, timeout_s=30))
    first, fresh1 = oracle.evaluate(good, 0)
    second, fresh2 = oracle.evaluate(good, 5)
    assert fresh1 and not fresh2
    assert second is first


def test_timeout_is_error(run_dir: tuple[Path, str, str]) -> None:
    run, good, _ = run_dir
    check = f'{PY} -c "import time; time.sleep(30)"'
    result, _ = Oracle(run, OracleConfig(check=check, timeout_s=0.5)).evaluate(good, 0)
    assert result.verdict is Verdict.ERROR
    assert result.runs[0].timed_out


def test_pytest_no_tests_is_error(run_dir: tuple[Path, str, str]) -> None:
    run, good, _ = run_dir
    check = f"{PY} -m pytest -q"  # scratch has no tests: exit code 5
    result, _ = Oracle(run, OracleConfig(check=check, timeout_s=60)).evaluate(good, 0)
    assert result.runs[0].exit_code == 5
    assert result.verdict is Verdict.ERROR
    assert result.n_runs == 2  # ERROR_RUNS_LIMIT


def test_output_tail_is_bounded(tmp_path: Path) -> None:
    check = f"{PY} -c \"print('x' * 10000); print('END')\""
    out = execute_check(check, tmp_path, 30)
    assert out.exit_code == 0
    assert len(out.output_tail) <= 2000
    assert out.output_tail.rstrip().endswith("END")


def test_bin_dir_leads_path(tmp_path: Path) -> None:
    out = execute_check('python -c "import sys; print(sys.prefix)"', tmp_path, 30)
    assert out.exit_code == 0
    assert Path(sys.prefix).name in out.output_tail


def test_repeats_follow_the_decider(run_dir: tuple[Path, str, str]) -> None:
    run, good, bad = run_dir
    policy = OraclePolicy(flake_rate=0.1, max_runs=3, per_probe_error=0.01, rule="test")
    calls: list[tuple[int, int]] = []

    def decide(p: OraclePolicy, n_fail: int, n_runs: int) -> Verdict | None:
        calls.append((n_fail, n_runs))
        if n_runs < 3:
            return None
        return Verdict.FAIL if n_fail >= 2 else Verdict.PASS

    oracle = Oracle(run, OracleConfig(check=CHECK, timeout_s=30), policy, decide)
    result, _ = oracle.evaluate(bad, 1)
    assert result.n_runs == 3
    assert result.verdict is Verdict.FAIL
    assert calls == [(1, 1), (2, 2), (3, 3)]


def test_decider_that_never_decides_still_stops(run_dir: tuple[Path, str, str]) -> None:
    run, good, _ = run_dir
    policy = OraclePolicy(flake_rate=0.0, max_runs=2, per_probe_error=0.0, rule="test")
    oracle = Oracle(run, OracleConfig(check=CHECK, timeout_s=30), policy, lambda p, f, n: None)
    result, _ = oracle.evaluate(good, 0)
    assert result.n_runs == 2
    assert result.verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("run", "text"),
    [
        (OracleRun(exit_code=127, duration_s=0), "check command not found (exit 127)"),
        (OracleRun(exit_code=126, duration_s=0), "not executable (exit 126)"),
        (OracleRun(exit_code=5, duration_s=0), "no tests matched the check (pytest exit 5)"),
        (
            OracleRun(exit_code=4, duration_s=0, output_tail="ERROR: not found: tests/x.py::t"),
            "test ID not found (pytest exit 4)",
        ),
        (OracleRun(exit_code=4, duration_s=0, output_tail="bad option"), "usage error (exit 4)"),
        (OracleRun(exit_code=3, duration_s=0), "internal error (exit 3)"),
        (OracleRun(exit_code=None, duration_s=1, timed_out=True), "timed out"),
        (OracleRun(exit_code=9, duration_s=0), "exit 9"),
    ],
)
def test_describe_error(run: OracleRun, text: str) -> None:
    assert text in describe_error(run)


def test_describe_error_names_the_timeout() -> None:
    run = OracleRun(exit_code=None, duration_s=1, timed_out=True)
    assert describe_error(run, 120.0) == "check timed out after 120s"


def test_run_command_returns_full_output_while_the_run_keeps_a_tail(tmp_path: Path) -> None:
    code = "print('x' * 5000); print('END')"
    run, full = run_command(f'{PY} -c "{code}"', tmp_path, 30)
    assert run.exit_code == 0
    assert len(full) > 5000 and len(run.output_tail) == 2000
    assert run.output_tail.endswith("END\n") and full.endswith("END\n")
