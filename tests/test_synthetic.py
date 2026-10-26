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
