import json
import math
import random
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentbisect import snapshot, stats
from agentbisect.bisect import bisect
from agentbisect.cli import app
from agentbisect.models import (
    BisectStatus,
    OracleConfig,
    OracleRun,
    RunReport,
    Step,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)
from agentbisect.synthetic import flaky_check, make_synthetic_run

CHECK = "python -m pytest -q tests/test_calc.py::test_add"
N = 30


def run_of(code: int | None, timed_out: bool = False) -> OracleRun:
    return OracleRun(exit_code=code, duration_s=0.0, timed_out=timed_out)


# --------------------------------------------------------------------------- real oracle


@pytest.mark.parametrize("k", [1, 15, 30])
def test_finds_break_and_respects_probe_bound(tmp_path: Path, k: int) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=k)
    result = bisect(trace, run, OracleConfig(check=CHECK))
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == k
    assert result.precondition_probes == 2
    assert result.n_probes <= math.ceil(math.log2(N)) + result.precondition_probes
    assert result.confidence is not None and result.confidence > 0.99
    assert not result.non_monotone and result.transitions == [k]


def test_noop_steps_are_never_probed(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=20, break_at=12, noop_every=4)
    result = bisect(trace, run, OracleConfig(check=CHECK))
    assert result.decisive_step == 12
    probed = [p.snapshot for p in result.probes]
    assert len(probed) == len(set(probed))  # no snapshot is decided twice
    # steps 4, 8, 16, 20 change nothing, so 16 candidates remain: 4 bisect probes + 2 endpoints
    assert result.n_probes <= 6


def test_not_bisectable_when_step0_fails(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=5, break_at=2)
    result = bisect(trace, run, OracleConfig(check="python -c 'import sys; sys.exit(1)'"))
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "step 0" in (result.reason or "")
    assert result.decisive_step is None


def test_not_bisectable_when_final_step_passes(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=6, break_at=2, fix_at=5)
    result = bisect(trace, run, OracleConfig(check=CHECK))
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "final step" in (result.reason or "")


def test_oracle_error_is_not_bisectable(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=5, break_at=2)
    result = bisect(trace, run, OracleConfig(check="python -m pytest -q tests/nope.py"))
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "error" in (result.reason or "")


def test_empty_trace_and_identical_endpoints(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=3, break_at=1)
    empty = Trace(meta=trace.meta, steps=[])
    assert bisect(empty, run, OracleConfig(check=CHECK)).status is BisectStatus.NOT_BISECTABLE
    same = Trace(
        meta=trace.meta,
        steps=[Step(index=1, tool="read_file", kind=ToolKind.READ)],
    )
    result = bisect(same, run, OracleConfig(check=CHECK))
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "identical" in (result.reason or "")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def test_real_git_untouched_through_the_cli(tmp_path: Path) -> None:
    run = tmp_path / "run"
    make_synthetic_run(run, n_steps=10, break_at=6, git_workdir=True)
    work = run / "workdir"
    before = (_git(work, "log", "--format=%H"), _git(work, "status", "--porcelain"))
    index_before = (work / ".git" / "index").read_bytes()
    result = CliRunner().invoke(app, ["run", "--trace", str(run), "--check", CHECK])
    assert result.exit_code == 0, result.output
    assert (_git(work, "log", "--format=%H"), _git(work, "status", "--porcelain")) == before
    assert (work / ".git" / "index").read_bytes() == index_before


def test_cli_writes_bisect_json(tmp_path: Path) -> None:
    run = tmp_path / "run"
    make_synthetic_run(run, n_steps=12, break_at=7)
    result = CliRunner().invoke(app, ["run", "--trace", str(run), "--check", CHECK])
    assert result.exit_code == 0, result.output
    assert "Step 7" in result.output
    report = RunReport.model_validate(json.loads((run / "bisect.json").read_text()))
    culprit = report.culprits[0]
    assert culprit.bisect.decisive_step == 7
    assert culprit.step is not None and culprit.step.index == 7
    assert "-    return a + b" in culprit.diff and "+    return a - b" in culprit.diff


def test_cli_exit_codes(tmp_path: Path) -> None:
    run = tmp_path / "run"
    make_synthetic_run(run, n_steps=4, break_at=2)
    runner = CliRunner()
    assert runner.invoke(app, ["run", "--trace", str(run)]).exit_code == 2
    bad = runner.invoke(
        app, ["run", "--trace", str(run), "--check", "python -c 'raise SystemExit(1)'"]
    )
    assert bad.exit_code == 1
    status = json.loads((run / "bisect.json").read_text())["culprits"][0]["bisect"]["status"]
    assert status == "not_bisectable"


@pytest.mark.slow
def test_flaky_real_oracle_still_finds_the_step(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=10, break_at=4, flaky_p=0.1, seed=7)
    check = (run / "check.txt").read_text().strip()
    assert check == flaky_check(run, 0.1, 7)
    result = bisect(trace, run, OracleConfig(check=check))
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == 4


# --------------------------------------------------------------------------- simulated oracle
# A simulated oracle (the `executor` seam) lets us run hundreds of bisects with no subprocess.


@pytest.fixture(scope="module")
def sim_trace(tmp_path_factory: pytest.TempPathFactory) -> tuple[Trace, Path, dict[str, int]]:
    run = tmp_path_factory.mktemp("sim") / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=1)
    index = {trace.meta.base_snapshot: 0} | {s.snapshot: s.index for s in trace.steps if s.snapshot}
    return trace, run, index


def simulated(index: dict[str, int], truth, p: float, seed: int, counter: list[int]):
    rng = random.Random(seed)

    def executor(sha: str) -> OracleRun:
        counter[0] += 1
        fails = truth(index[sha])
        if rng.random() < p:
            fails = not fails
        return run_of(1 if fails else 0)

    return executor


@pytest.mark.parametrize("k", range(1, N + 1))
def test_simulated_deterministic_every_break_point(sim_trace, k: int) -> None:
    trace, run, index = sim_trace
    runs = [0]
    result = bisect(
        trace,
        run,
        OracleConfig(check="x"),
        executor=simulated(index, lambda i: i >= k, 0.0, 0, runs),
    )
    assert result.decisive_step == k
    assert result.n_probes <= stats.ceil_log2(N) + result.precondition_probes
    assert runs[0] <= 6 + stats.ceil_log2(N) + 6  # METHODS §2.5 cost bound: 6 + <=5 + 6
    assert result.policy is not None and result.policy.flake_rate == 0.0


def test_simulated_flaky_error_rate_within_budget(sim_trace) -> None:
    trace, run, index = sim_trace
    rng = random.Random(123)
    wrong = refused = 0
    trials = 200
    for seed in range(trials):
        k = rng.randint(1, N)
        result = bisect(
            trace,
            run,
            OracleConfig(check="x"),
            executor=simulated(index, lambda i, k=k: i >= k, 0.1, seed, [0]),
        )
        if result.status is not BisectStatus.FOUND:
            refused += 1
        elif result.decisive_step != k:
            wrong += 1
    assert wrong / trials <= 0.05  # METHODS §2.4: <= 5% wrong at p = 0.1 (about 1% expected)
    assert refused / trials <= 0.05


def test_simulated_too_flaky_is_refused(sim_trace) -> None:
    trace, run, index = sim_trace
    result = bisect(
        trace,
        run,
        OracleConfig(check="x"),
        executor=simulated(index, lambda i: i >= 10, 0.45, 5, [0]),
    )
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "flaky" in (result.reason or "")


def test_simulated_precondition_failure_is_unanimous(sim_trace) -> None:
    trace, run, index = sim_trace
    always_fail = bisect(trace, run, OracleConfig(check="x"), executor=lambda sha: run_of(1))
    assert always_fail.status is BisectStatus.NOT_BISECTABLE
    assert "step 0" in (always_fail.reason or "")
    always_pass = bisect(trace, run, OracleConfig(check="x"), executor=lambda sha: run_of(0))
    assert "final step" in (always_pass.reason or "")


def test_simulated_errors_are_skipped_like_git_bisect_skip(sim_trace) -> None:
    trace, run, index = sim_trace

    def executor(sha: str) -> OracleRun:
        i = index[sha]
        if i == 15:
            return run_of(None, timed_out=True)  # the first midpoint cannot be tested
        return run_of(1 if i >= 20 else 0)

    result = bisect(trace, run, OracleConfig(check="x"), executor=executor)
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == 20


def test_simulated_untestable_range_is_named(sim_trace) -> None:
    trace, run, index = sim_trace

    def executor(sha: str) -> OracleRun:
        i = index[sha]
        if i == 0:
            return run_of(0)
        if i == N:
            return run_of(1)
        return run_of(3)  # pytest internal error everywhere in between

    result = bisect(trace, run, OracleConfig(check="x"), executor=executor)
    assert result.status is BisectStatus.NOT_BISECTABLE
    assert "1..30" in (result.reason or "")


def test_collection_error_counts_as_a_break(sim_trace) -> None:
    trace, run, index = sim_trace
    result = bisect(
        trace,
        run,
        OracleConfig(check="x"),
        executor=lambda sha: run_of(2 if index[sha] >= 9 else 0),
    )
    assert result.decisive_step == 9


# --------------------------------------------------------------------------- non-monotone


def _repeating_trace(tmp_path: Path, contents: list[str]) -> tuple[Trace, Path]:
    """A trace whose steps write ``contents`` into v.txt; repeated contents repeat the tree."""
    run, work = tmp_path / "run", tmp_path / "work"
    work.mkdir()
    (work / "v.txt").write_text("a")
    base = snapshot.init_shadow(run, work)
    steps = []
    for i, text in enumerate(contents, 1):
        (work / "v.txt").write_text(text)
        sha, tree = snapshot.take_snapshot(run, work, f"step {i}")
        steps.append(Step(index=i, tool="write_file", kind=ToolKind.WRITE, snapshot=sha, tree=tree))
    meta = TraceMeta(run_id="r", source=TraceSource.SYNTHETIC, workdir=work, base_snapshot=base)
    return Trace(meta=meta, steps=steps), run


CHECK_A = "python -c \"import sys; sys.exit(0 if open('v.txt').read() == 'a' else 1)\""


def test_non_monotone_history_reports_every_transition(tmp_path: Path) -> None:
    # a(pass) b(fail) a(pass: repaired) b(fail again) -> breaks at steps 1 and 3
    trace, run = _repeating_trace(tmp_path, ["b", "a", "b"])
    result = bisect(trace, run, OracleConfig(check=CHECK_A))
    assert result.status is BisectStatus.FOUND
    assert result.non_monotone
    assert result.transitions == [1, 3]
    assert result.decisive_step == 3  # the break the final state still carries (§3.1)


def test_repeated_tree_with_one_transition_stays_monotone(tmp_path: Path) -> None:
    # Passes while the file is not 'b'. Tree 'a' repeats, but only left of the boundary.
    trace, run = _repeating_trace(tmp_path, ["c", "a", "b"])
    check = "python -c \"import sys; sys.exit(1 if open('v.txt').read() == 'b' else 0)\""
    result = bisect(trace, run, OracleConfig(check=check))
    assert result.decisive_step == 3
    assert not result.non_monotone
    assert result.transitions == [3]
