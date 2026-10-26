import subprocess
import sys
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.models import Trace
from agentbisect.synthetic import flaky_exit_code, make_synthetic_run


def test_trace_shape_and_roundtrip(tmp_path: Path) -> None:
    run = tmp_path / "r"
    trace = make_synthetic_run(run, n_steps=8, break_at=3)
    assert trace.n == 8
    assert [s.index for s in trace.steps] == list(range(1, 9))
    assert len({s.snapshot for s in trace.steps}) == 8  # every step changes the repo
    assert Trace.load(run) == trace
    assert (run / "check.txt").read_text().strip().startswith("python -m pytest")
    assert trace.meta.sabotage_step == 3


def test_break_step_is_the_only_failing_edit(tmp_path: Path) -> None:
    run = tmp_path / "r"
    trace = make_synthetic_run(run, n_steps=6, break_at=4, fix_at=6)
    src = [
        snapshot.read_file(run, trace.snapshot_at(i), "calc/__init__.py") or "" for i in range(7)
    ]
    bad = ["a - b" in text for text in src]
    assert bad == [False, False, False, False, True, True, False]


def test_noop_steps_are_deduplicated(tmp_path: Path) -> None:
    trace = make_synthetic_run(tmp_path / "r", n_steps=10, break_at=3, noop_every=5)
    assert trace.steps[4].snapshot == trace.steps[3].snapshot
    assert trace.steps[9].snapshot == trace.steps[8].snapshot
    assert len({s.snapshot for s in trace.steps}) == 8


def test_invalid_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_synthetic_run(tmp_path / "a", n_steps=5, break_at=6)
    with pytest.raises(ValueError):
        make_synthetic_run(tmp_path / "b", n_steps=5, break_at=3, fix_at=3)


def test_flaky_counter_is_deterministic(tmp_path: Path) -> None:
    def sequence(state: Path) -> list[int]:
        return [flaky_exit_code(0, 0.3, 7, state)[0] for _ in range(40)]

    a = sequence(tmp_path / "a.counter")
    b = sequence(tmp_path / "b.counter")
    assert a == b
    assert 0 < sum(a) < 40  # some flips, not all
    assert flaky_exit_code(5, 1.0, 0, tmp_path / "c.counter")[0] == 5  # only 0/1 are flipped


def test_flaky_cli_wrapper(tmp_path: Path) -> None:
    state = tmp_path / "s"
    cmd = [sys.executable, "-m", "agentbisect.synthetic", "flaky", "--p", "1.0", "--seed", "1"]
    cmd += ["--state", str(state), "--", sys.executable, "-c", "pass"]
    assert subprocess.run(cmd, capture_output=True).returncode == 1
    assert state.read_text() == "1"
    cmd[cmd.index("1.0")] = "0.0"
    assert subprocess.run(cmd, capture_output=True).returncode == 0


def test_make_cli_prints_check(tmp_path: Path) -> None:
    out = tmp_path / "r"
    proc = subprocess.run(
        [sys.executable, "-m", "agentbisect.synthetic", "make", "--out", str(out)]
        + ["--steps", "5", "--break-at", "2"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "agentbisect run --trace" in proc.stdout and "--check" in proc.stdout


def test_observe_at_makes_a_failing_shell_step(tmp_path: Path) -> None:
    trace = make_synthetic_run(tmp_path / "r", n_steps=8, break_at=3, observe_at=5)
    step = trace.steps[4]
    assert (step.kind.value, step.exit_code) == ("shell", 1)
    assert "FAILED tests/test_calc.py::test_add" in step.output
    assert step.snapshot == trace.steps[3].snapshot  # running the test changes nothing
    assert all(s.exit_code is None for i, s in enumerate(trace.steps) if i != 4)


def test_observe_in_the_breaking_step_edits_and_fails(tmp_path: Path) -> None:
    run = tmp_path / "r"
    trace = make_synthetic_run(run, n_steps=6, break_at=3, observe_at=3)
    step = trace.steps[2]
    assert step.kind.value == "shell" and step.exit_code == 1
    assert step.snapshot != trace.steps[1].snapshot  # it also made the break
    src = snapshot.read_file(run, step.snapshot, "calc/__init__.py") or ""
    assert "a - b" in src


def test_break_again_makes_a_second_different_bug_without_a_repeated_tree(tmp_path: Path) -> None:
    run = tmp_path / "r"
    trace = make_synthetic_run(run, n_steps=10, break_at=3, fix_at=5, break_again_at=8)
    assert len({s.tree for s in trace.steps}) == 10
    src = [
        snapshot.read_file(run, trace.snapshot_at(i), "calc/__init__.py") or "" for i in range(11)
    ]
    assert ["a - b" in t for t in src[3:5]] == [True, True]
    assert "a + b" in src[5] and "a * b" in src[8] and "a * b" in src[10]
    assert trace.meta.sabotage_step == 8


def test_observe_and_break_again_validation(tmp_path: Path) -> None:
    for kwargs in (
        {"observe_at": 2},  # before the break
        {"observe_at": 5, "fix_at": 4},  # healthy after the fix
        {"break_again_at": 5},  # needs fix_at
        {"fix_at": 4, "break_again_at": 4},
        {"fix_at": 4, "break_again_at": 9},
    ):
        with pytest.raises(ValueError):
            make_synthetic_run(tmp_path / "x", n_steps=8, break_at=3, **kwargs)


def test_defaults_are_unchanged_by_the_new_options(tmp_path: Path) -> None:
    trace = make_synthetic_run(tmp_path / "r", n_steps=4, break_at=2)
    assert [(s.tokens_in, s.tokens_out, s.output, s.exit_code) for s in trace.steps] == [
        (100, 50, "ok", None)
    ] * 4
    noop = make_synthetic_run(tmp_path / "n", n_steps=4, break_at=2, noop_every=4)
    assert noop.steps[3].kind.value == "shell" and noop.steps[3].exit_code == 0


def test_make_cli_accepts_new_flags(tmp_path: Path) -> None:
    out = tmp_path / "r"
    cmd = [sys.executable, "-m", "agentbisect.synthetic", "make", "--out", str(out)]
    cmd += ["--steps", "9", "--break-at", "2", "--fix-at", "4", "--break-again-at", "6"]
    cmd += ["--observe-at", "7", "--token-growth", "5"]
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    trace = Trace.load(out)
    assert trace.steps[6].exit_code == 1 and trace.steps[1].tokens_in == 105
