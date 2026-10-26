from pathlib import Path

import pytest

from agentbisect.analyze import SignalMatcher, analyze, find_observation, parse_check
from agentbisect.models import BisectResult, BisectStatus, Step, ToolKind
from agentbisect.synthetic import make_synthetic_run, pytest_failure_output

CHECK = "python -m pytest -q tests/test_calc.py::test_add"
N = 30


def found(k: int, check: str = CHECK) -> BisectResult:
    return BisectResult(
        check=check,
        status=BisectStatus.FOUND,
        decisive_step=k,
        transitions=[k],
        precondition_probes=2,
    )


def shell(output: str, exit_code: int | None = 1, index: int = 1) -> Step:
    return Step(
        index=index, tool="run_shell", kind=ToolKind.SHELL, output=output, exit_code=exit_code
    )


# --------------------------------------------------------------------------- test keys (§4.1)


def test_parse_node_ids() -> None:
    refs = parse_check("python -m pytest -q tests/auth/test_x.py::TestC::test_f[1-2] -x")
    assert len(refs) == 1
    ref = refs[0]
    assert (ref.file, ref.key) == ("test_x.py", "test_x.py::TestC::test_f")
    assert (ref.function, ref.qualified) == ("test_f", "TestC.test_f")


def test_parse_plain_function_files_and_none() -> None:
    (ref,) = parse_check("pytest tests/test_a.py::test_f")
    assert (ref.key, ref.qualified) == ("test_a.py::test_f", "test_f")
    (file_ref,) = parse_check("python -m pytest -q tests/test_a.py")
    assert (file_ref.key, file_ref.function) == ("test_a.py", None)
    assert parse_check("python -m pytest -q") == []
    assert parse_check("python script.py") == []  # not pytest: .py is not a test file
    assert len(parse_check("pytest 'a/test_x.py::test_p[a b]' b/test_y.py::test_q")) == 2


# --------------------------------------------------------------------------- signal (§4.2)

TOUCHED = ["src/auth/session.py"]
LOGIN = "python -m pytest -q tests/test_auth.py::TestAuth::test_login"


@pytest.mark.parametrize(
    ("check", "line"),
    [
        (LOGIN, "FAILED tests/test_auth.py::TestAuth::test_login - AssertionError"),  # P1
        (LOGIN, "ERROR tests/test_auth.py::TestAuth::test_login - fixture 'db' not found"),
        (LOGIN, "FAILED tests/test_auth.py::TestAuth::test_login[admin] - boom"),
        (LOGIN, "_______ TestAuth.test_login _______"),  # P2
        (LOGIN, "_______ TestAuth.test_login[admin] _______"),
        ("pytest tests/test_auth.py::test_login", "____ test_login ____"),
        (LOGIN, "____ ERROR collecting tests/test_auth.py ____"),  # P3
        (LOGIN, 'File "/w/src/auth/session.py", line 41, in login'),  # P4
        (LOGIN, '  File "src/auth/session.py", line 3, in <module>'),
        (LOGIN, "src/auth/session.py:41: AssertionError"),  # P5
        ("pytest tests/test_auth.py", "FAILED tests/test_auth.py::test_other - x"),  # file key
    ],
)
def test_signal_patterns_match(check: str, line: str) -> None:
    step = shell(f"collecting ...\n{line}\n1 failed in 0.1s\n")
    assert SignalMatcher(check, TOUCHED).signal(step) == line.strip()


@pytest.mark.parametrize(
    ("check", "line"),
    [
        (LOGIN, "FAILED tests/test_auth.py::TestAuth::test_login2 - x"),  # longer name
        (LOGIN, "FAILED tests/test_auth.py::TestOther::test_login - x"),  # other class
        (LOGIN, "PASSED tests/test_auth.py::TestAuth::test_login"),  # no FAILED/ERROR word
        (LOGIN, "FAILED tests/test_other.py::test_thing - x"),  # other test
        (LOGIN, "_______ TestOther.test_login _______"),
        (LOGIN, "_______ test_login _______"),  # class-qualified name required
        (LOGIN, "____ ERROR collecting tests/test_other.py ____"),
        (LOGIN, 'File "/w/src/auth/other_session.py", line 41, in login'),  # not whole components
        (LOGIN, 'File "/w/lib/auth/session.py", line 41, in login'),  # not the touched path
        (LOGIN, "src/auth/other.py:41: AssertionError"),
        (LOGIN, "ERROR mytest_auth.py::TestAuth::test_login"),  # key must stand alone
    ],
)
def test_signal_patterns_reject(check: str, line: str) -> None:
    assert SignalMatcher(check, TOUCHED).signal(shell(line)) is None


def test_signal_needs_shell_step_and_nonzero_exit() -> None:
    line = "FAILED tests/test_auth.py::TestAuth::test_login - x"
    matcher = SignalMatcher(LOGIN, TOUCHED)
    edit = Step(index=1, tool="edit_file", kind=ToolKind.EDIT, output=line, exit_code=1)
    read = Step(index=1, tool="read_file", kind=ToolKind.READ, output=line)
    assert matcher.signal(edit) is None
    assert matcher.signal(read) is None
    assert matcher.signal(shell(line, exit_code=0)) is None
    assert matcher.signal(shell(line, exit_code=None)) == line  # unknown exit code is allowed
    assert matcher.signal(shell(line, exit_code=2)) == line


def test_signal_is_first_matching_line_cut_to_200_chars() -> None:
    long_line = "FAILED tests/test_auth.py::TestAuth::test_login - " + "x" * 400
    out = "noise\n" + long_line + "\nsrc/auth/session.py:41: AssertionError\n"
    signal = SignalMatcher(LOGIN, TOUCHED).signal(shell(out))
    assert signal is not None and signal.startswith("FAILED") and len(signal) == 200


def test_traceback_rule_ignores_python_files_not_touched() -> None:
    out = 'Traceback (most recent call last):\n  File "/w/shop/pricing.py", line 9, in f\n'
    assert SignalMatcher("pytest", ["shop/orders.py"]).signal(shell(out)) is None
    assert SignalMatcher("pytest", ["shop/pricing.py"]).signal(shell(out)) is not None
    assert SignalMatcher("pytest", ["README.md"]).signal(shell(out)) is None  # only *.py count


def test_whole_suite_check_accepts_any_failure_line() -> None:
    matcher = SignalMatcher("python -m pytest -q", [])
    assert matcher.signal(shell("FAILED tests/test_a.py::test_x - boom")) is not None
    assert matcher.signal(shell("____ test_x ____")) is not None
    assert matcher.signal(shell("3 passed")) is None


# --------------------------------------------------------------------------- lag, end to end


def test_observed_three_steps_after_is_lag_three(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=15, observe_at=18)
    a = analyze(trace, found(15), run)
    assert (a.t_err, a.t_obs, a.lag, a.censored) == (15, 18, 3, False)
    assert a.steps_after_err == 15
    assert a.wasted_tokens == 15 * 150 and a.total_tokens == 30 * 150  # 150 tokens per step
    assert a.wasted_share == 0.5
    assert a.observed_signal is not None and "test_add" in a.observed_signal


def test_observed_in_the_decisive_step_is_lag_zero(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=10, break_at=4, observe_at=4)
    a = analyze(trace, found(4), run)
    assert (a.t_obs, a.lag, a.censored) == (4, 0, False)
    assert trace.steps[3].kind is ToolKind.SHELL and trace.steps[3].exit_code == 1


def test_never_observed_is_censored_with_exposure(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=12)
    a = analyze(trace, found(12), run)
    assert (a.t_obs, a.lag, a.observed_signal) == (None, None, None)
    assert a.censored and a.steps_after_err == N - 12
    assert a.wasted_tokens == (N - 12) * 150


def test_break_at_last_step_is_censored_with_zero_exposure(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=8, break_at=8)
    a = analyze(trace, found(8), run)
    assert a.censored and a.steps_after_err == 0 and a.wasted_tokens == 0


def test_signal_before_t_err_is_ignored(tmp_path: Path) -> None:
    # broke at 3, noticed at 4, fixed at 6, broke again at 9; the last break is t_err = 9
    run = tmp_path / "run"
    trace = make_synthetic_run(
        run, n_steps=12, break_at=3, fix_at=6, break_again_at=9, observe_at=4
    )
    assert analyze(trace, found(3), run).lag == 1
    late = analyze(trace, found(9), run)
    assert late.censored and late.steps_after_err == 3

    again = make_synthetic_run(
        tmp_path / "run2", n_steps=12, break_at=3, fix_at=6, break_again_at=9, observe_at=11
    )
    assert analyze(again, found(9), tmp_path / "run2").lag == 2


def test_token_growth_gives_hand_computed_waste(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=10, break_at=4, token_growth=10)
    # step i costs 150 + 10 (i - 1) tokens
    assert [s.tokens for s in trace.steps[:3]] == [150, 160, 170]
    a = analyze(trace, found(4), run)
    assert a.total_tokens == 10 * 150 + 10 * 45
    assert a.wasted_tokens == 6 * 150 + 10 * (4 + 5 + 6 + 7 + 8 + 9)


def test_traceback_in_untouched_file_does_not_count(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=10, break_at=4)
    steps = list(trace.steps)
    other = 'Traceback:\n  File "/w/elsewhere/mod.py", line 3, in f\nValueError\n'
    steps[6] = steps[6].model_copy(update={"kind": ToolKind.SHELL, "output": other, "exit_code": 1})
    touched = trace.model_copy(update={"steps": steps})
    assert analyze(touched, found(4, "python -m pytest -q"), run).censored
    hit = 'Traceback:\n  File "/w/calc/__init__.py", line 3, in add\nValueError\n'
    steps[7] = steps[7].model_copy(update={"kind": ToolKind.SHELL, "output": hit, "exit_code": 1})
    a = analyze(trace.model_copy(update={"steps": steps}), found(4, "python -m pytest -q"), run)
    assert a.t_obs == 8  # calc/__init__.py is what step 4 touched


def test_find_observation_scans_from_t_err() -> None:
    steps = [
        shell("FAILED tests/test_x.py::test_a - x", index=1),
        shell("ok", exit_code=0, index=2),
        shell("FAILED tests/test_x.py::test_a - y", index=3),
    ]
    assert find_observation(steps, 2, "pytest tests/test_x.py::test_a", []) == (
        3,
        "FAILED tests/test_x.py::test_a - y",
    )
    assert find_observation(steps, 4, "pytest tests/test_x.py::test_a", []) == (None, None)


def test_analyze_rejects_results_that_are_not_found(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=5, break_at=2)
    bad = BisectResult(check=CHECK, status=BisectStatus.NOT_BISECTABLE, reason="x")
    with pytest.raises(ValueError, match="FOUND"):
        analyze(trace, bad, run)
    with pytest.raises(ValueError, match="outside"):
        analyze(trace, found(9), run)


def test_failure_output_is_a_signal_for_its_own_check() -> None:
    out = pytest_failure_output(-1)
    assert SignalMatcher(CHECK, []).signal(shell(out)) is not None


def test_whole_suite_check_matches_verbose_failure_line():
    from agentbisect.analyze import SignalMatcher

    m = SignalMatcher("python -m pytest -q", [])
    assert m._line_matches("tests/test_x.py::test_f FAILED                [ 50%]")
    assert not m._line_matches("tests/test_x.py::test_f PASSED                [ 50%]")


def test_colored_pytest_output_is_parsed():
    from agentbisect.analyze import SignalMatcher, strip_ansi
    from agentbisect.bisect import parse_outcomes

    colored = "\x1b[32mPASSED\x1b[0m tests/t.py::\x1b[1mtest_a\x1b[0m\n\x1b[31mFAILED\x1b[0m tests/t.py::test_b - x\n"
    assert strip_ansi("\x1b[32m\x1b[1m746 passed\x1b[0m") == "746 passed"
    assert parse_outcomes(colored) == {
        "tests/t.py::test_a": "PASSED",
        "tests/t.py::test_b": "FAILED",
    }
    assert SignalMatcher("python -m pytest -q tests/t.py::test_b", [])._line_matches(
        strip_ansi(colored.splitlines()[1])
    )
