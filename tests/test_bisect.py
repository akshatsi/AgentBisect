import json
import math
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentbisect import snapshot, stats
from agentbisect.bisect import (
    CAP_NOTE,
    MAX_AUTO_CULPRITS,
    SUITE_CMD,
    bisect,
    bisect_run,
    discover_failing_tests,
    newly_failing,
    parse_outcomes,
)
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
    assert runner.invoke(app, ["run", "--trace", str(tmp_path / "missing")]).exit_code == 2
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


# --------------------------------------------------------------------------- reasons


def _reason(tmp_path: Path, check: str, timeout_s: float = 30.0) -> str:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=4, break_at=2)
    result = bisect(trace, run, OracleConfig(check=check, timeout_s=timeout_s))
    assert result.status is BisectStatus.NOT_BISECTABLE
    return result.reason or ""


def test_reason_command_not_found(tmp_path: Path) -> None:
    reason = _reason(tmp_path, "definitely_not_a_real_command_xyz --flag")
    assert "oracle error at step 0" in reason and "not found (exit 127)" in reason


def test_reason_test_id_not_found(tmp_path: Path) -> None:
    reason = _reason(tmp_path, "python -m pytest -q tests/test_calc.py::test_nope")
    assert "test ID not found (pytest exit 4)" in reason


def test_reason_no_tests_matched(tmp_path: Path) -> None:
    reason = _reason(tmp_path, "python -m pytest -q tests/test_calc.py -k no_such_name")
    assert "no tests matched the check (pytest exit 5)" in reason


def test_reason_timeout(tmp_path: Path) -> None:
    reason = _reason(tmp_path, "sleep 5", timeout_s=0.2)
    assert "check timed out after 0.2s" in reason


def test_reason_names_final_step_error(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=4, break_at=2)
    last = trace.snapshot_at(4)

    def executor(sha: str) -> OracleRun:
        return run_of(127) if sha == last else run_of(0)

    result = bisect(trace, run, OracleConfig(check="x"), executor=executor)
    assert "oracle error at step 4" in (result.reason or "")
    assert "exit 127" in (result.reason or "")


# --------------------------------------------------------------------------- bisect_run


def test_bisect_run_explicit_check_fills_the_culprit(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=15, observe_at=18)
    report = bisect_run(trace, run, CHECK)
    assert (report.run_id, report.checks, report.auto_oracle) == (trace.meta.run_id, [CHECK], False)
    (culprit,) = report.culprits
    assert culprit.bisect.decisive_step == 15
    assert culprit.step is not None and culprit.step.index == 15
    assert "-    return a + b" in culprit.diff and "+    return a - b" in culprit.diff
    assert culprit.analysis is not None
    assert (culprit.analysis.t_err, culprit.analysis.t_obs, culprit.analysis.lag) == (15, 18, 3)
    RunReport.model_validate_json(report.model_dump_json())  # what the CLI writes


def test_break_the_agent_saw_and_fixed_is_reported_as_repaired(tmp_path: Path) -> None:
    # Break at 9, the agent's pytest at 13 shows it, a fix at 18: the run ends green.
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=24, break_at=9, observe_at=13, fix_at=18)
    for check in (None, CHECK):  # auto-oracle, then an explicit check
        report = bisect_run(trace, run, check)
        # auto: nothing fails at the end; explicit: the check passes at the end, not bisectable
        assert [c.bisect.status for c in report.culprits] == (
            [] if check is None else [BisectStatus.NOT_BISECTABLE]
        )
        (ep,) = report.repaired
        assert (ep.t_err, ep.seen_at, ep.lag, ep.reason) == (9, 13, 4, None)
        assert ep.check == f"{SUITE_CMD} tests/test_calc.py::test_add"
        assert ep.wasted_tokens == sum(s.tokens for s in trace.steps[9:13])
        RunReport.model_validate_json(report.model_dump_json())


def test_break_still_live_at_the_end_is_a_culprit_not_a_repair(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=24, break_at=9, observe_at=13)
    for check in (None, CHECK):
        report = bisect_run(trace, run, check)
        assert [c.bisect.decisive_step for c in report.culprits] == [9]
        assert report.repaired == []


def test_bisect_run_not_bisectable_has_no_analysis(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=5, break_at=2)
    report = bisect_run(trace, run, "python -c 'raise SystemExit(1)'")
    (culprit,) = report.culprits
    assert culprit.bisect.status is BisectStatus.NOT_BISECTABLE
    assert culprit.analysis is None and culprit.step is None and culprit.diff == ""


def test_undetected_non_monotone_without_repeated_tree(tmp_path: Path) -> None:
    """Broke at 5, fixed at 8, broke differently at 11: no tree repeats, so the default
    bisect (METHODS 3.2) lands on a transition without noticing. Pinned as a known limit."""
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=12, break_at=5, fix_at=8, break_again_at=11)
    assert len({s.tree for s in trace.steps}) == 12
    result = bisect_run(trace, run, CHECK).culprits[0].bisect
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == 5 and not result.non_monotone


# --------------------------------------------------------------------------- auto-oracle (fake)

TESTS = ["tests/t.py::test_a", "tests/t.py::test_b", "tests/t.py::test_c"]


def _outcomes(statuses: dict[str, str]) -> str:
    lines = [f"{v} {k}" + (" - assert 0" if v != "PASSED" else "") for k, v in statuses.items()]
    return "1 failed\n=== short test summary info ===\n" + "\n".join(lines) + "\n"


def _fake_world(trace: Trace, breaks: dict[str, int]):
    """Test ``name`` fails from step ``breaks[name]`` on. Returns (suite_runner, executor_factory)."""
    index = {trace.meta.base_snapshot: 0}
    index.update({s.snapshot: s.index for s in trace.steps})

    def status(node: str, i: int) -> str:
        return "FAILED" if node in breaks and i >= breaks[node] else "PASSED"

    def suite(sha: str) -> tuple[OracleRun, str]:
        out = _outcomes({t: status(t, index[sha]) for t in TESTS})
        failed = any(status(t, index[sha]) == "FAILED" for t in TESTS)
        return run_of(1 if failed else 0), out

    def factory(check: str):
        node = next(t for t in TESTS if t in check)
        return lambda sha: run_of(1 if status(node, index[sha]) == "FAILED" else 0)

    return suite, factory


def test_auto_oracle_bisects_each_newly_failing_test_separately(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=12, break_at=2)
    suite, factory = _fake_world(trace, {TESTS[0]: 4, TESTS[1]: 9})
    report = bisect_run(trace, run, None, suite_runner=suite, executor_factory=factory)
    assert report.auto_oracle
    assert report.checks == [f"{SUITE_CMD} {t}" for t in TESTS[:2]]
    assert [(c.check, c.bisect.decisive_step) for c in report.culprits] == [
        (f"{SUITE_CMD} {TESTS[0]}", 4),
        (f"{SUITE_CMD} {TESTS[1]}", 9),
    ]
    assert [c.step.index for c in report.culprits if c.step] == [4, 9]
    assert all(c.analysis and c.analysis.t_err == c.bisect.decisive_step for c in report.culprits)
    assert all(c.bisect.n_probes <= 6 for c in report.culprits)


def test_auto_oracle_orders_culprits_by_step_and_keeps_failures_last(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=12, break_at=2)
    # test_a breaks late, test_b early; test_c "fails" at the end only by a flake (never at 0)
    suite, factory = _fake_world(trace, {TESTS[0]: 10, TESTS[1]: 3, TESTS[2]: 12})

    def flaky_factory(check: str):
        inner = factory(check)
        if TESTS[2] in check:
            return lambda sha: run_of(0)  # the oracle never reproduces the final failure
        return inner

    report = bisect_run(trace, run, None, suite_runner=suite, executor_factory=flaky_factory)
    steps = [c.bisect.decisive_step for c in report.culprits]
    assert steps == [3, 10, None]
    assert report.culprits[2].bisect.status is BisectStatus.NOT_BISECTABLE
    assert "final step" in (report.culprits[2].bisect.reason or "")


def test_auto_oracle_nothing_newly_failing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=6, break_at=2)
    suite, factory = _fake_world(trace, {})
    report = bisect_run(trace, run, None, suite_runner=suite, executor_factory=factory)
    assert report.auto_oracle and report.checks == [] and report.culprits == []


@pytest.mark.parametrize(
    ("code", "text"),
    [(127, "not found (exit 127)"), (5, "no tests matched"), (None, "timed out")],
)
def test_auto_oracle_suite_cannot_run(tmp_path: Path, code: int | None, text: str) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=6, break_at=2)

    def suite(sha: str) -> tuple[OracleRun, str]:
        return run_of(code, timed_out=code is None), ""

    report = bisect_run(trace, run, None, suite_runner=suite)
    (culprit,) = report.culprits
    assert culprit.check == SUITE_CMD and report.checks == [SUITE_CMD]
    assert culprit.bisect.status is BisectStatus.NOT_BISECTABLE
    assert "step 0" in (culprit.bisect.reason or "") and text in (culprit.bisect.reason or "")


def test_auto_oracle_needs_a_passing_test_at_step_0(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=4, break_at=2)
    nodes, problem = discover_failing_tests(
        trace, run, 10.0, lambda sha: (run_of(1), _outcomes({TESTS[0]: "FAILED"}))
    )
    assert nodes == [] and problem is not None and "no test passes at step 0" in problem


def test_parse_outcomes_and_newly_failing() -> None:
    out = (
        "...F\n=== short test summary info ===\n"
        "PASSED tests/a.py::test_1\n"
        "PASSED tests/a.py::test_2[x y]\n"
        "FAILED tests/a.py::test_3 - assert 1 == 2 - extra\n"
        "ERROR tests/b.py - ImportError: no module named x\n"
        "ERROR tests/c.py::test_5 - fixture 'f' not found\n"
        "SKIPPED [1] tests/c.py:9: reason\n"
    )
    got = parse_outcomes(out)
    assert got == {
        "tests/a.py::test_1": "PASSED",
        "tests/a.py::test_2[x y]": "PASSED",
        "tests/a.py::test_3": "FAILED",
        "tests/b.py": "ERROR",
        "tests/c.py::test_5": "ERROR",
    }
    before = {
        "tests/a.py::test_1": "PASSED",
        "tests/a.py::test_3": "PASSED",
        "tests/a.py::test_9": "FAILED",  # failed before: not a new break
        "tests/b.py::test_4": "PASSED",  # its file no longer collects
        "tests/c.py::test_5": "PASSED",
        "tests/d.py::test_6": "PASSED",  # gone at the end: not a failure
    }
    assert newly_failing(before, got) == [
        "tests/a.py::test_3",
        "tests/b.py::test_4",
        "tests/c.py::test_5",
    ]


# --------------------------------------------------------------------------- auto-oracle (fixture)

ROOT = Path(__file__).resolve().parent.parent


def _sabotage(task: str) -> tuple[str, str, str]:
    import yaml

    spec = yaml.safe_load((ROOT / "tasks" / f"{task}.yaml").read_text())["sabotage"]
    return spec["file"], spec["find"], spec["replace"]


@pytest.mark.slow
def test_auto_oracle_on_fixture_repo_finds_two_culprits(tmp_path: Path) -> None:
    """Pricing sabotage (s01) at step 2 breaks four tests; orders sabotage (s03) at step 5 breaks one."""
    work, run = tmp_path / "work", tmp_path / "run"
    shutil.copytree(
        ROOT / "tasks" / "fixture_repo", work, ignore=shutil.ignore_patterns("__pycache__")
    )
    base = snapshot.init_shadow(run, work)
    pricing, orders = _sabotage("s01"), _sabotage("s03")
    steps: list[Step] = []
    for i in range(1, 7):
        kind, args = ToolKind.WRITE, {"path": f"shop/extra_{i}.py"}
        if i in (2, 5):
            file, find, replace = pricing if i == 2 else orders
            text = (work / file).read_text()
            assert find in text
            (work / file).write_text(text.replace(find, replace, 1))
            kind, args = ToolKind.EDIT, {"path": file}
        else:
            (work / args["path"]).write_text(f"VALUE = {i}\n")
        sha, tree = snapshot.take_snapshot(run, work, f"step {i}")
        steps.append(Step(index=i, tool="x", kind=kind, args=args, snapshot=sha, tree=tree))
    meta = TraceMeta(run_id="fx", source=TraceSource.SYNTHETIC, workdir=work, base_snapshot=base)
    trace = Trace(meta=meta, steps=steps)

    report = bisect_run(trace, run, None)
    assert report.auto_oracle
    by_step: dict[int, list[str]] = {}
    for c in report.culprits:
        assert c.bisect.status is BisectStatus.FOUND, c.bisect.reason
        by_step.setdefault(c.bisect.decisive_step, []).append(c.check)
    assert sorted(by_step) == [2, 5]
    assert len(by_step[2]) == 4 and len(by_step[5]) == 1
    assert "test_cancel_restores_stock" in by_step[5][0]
    assert all("shop/pricing.py" in c.diff for c in report.culprits if c.step.index == 2)
    assert all("shop/orders.py" in c.diff for c in report.culprits if c.step.index == 5)
    assert [c.bisect.decisive_step for c in report.culprits] == [2, 2, 2, 2, 5]


def test_relative_run_dir_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run = Path("runs") / "try"
    trace = make_synthetic_run(run, n_steps=8, break_at=5, observe_at=6)
    report = bisect_run(trace, run, CHECK)
    (culprit,) = report.culprits
    assert culprit.bisect.status is BisectStatus.FOUND, culprit.bisect.reason
    assert culprit.bisect.decisive_step == 5 and culprit.analysis and culprit.analysis.lag == 1
    assert bisect(trace, run, OracleConfig(check=CHECK)).decisive_step == 5

    suite, factory = _fake_world(trace, {TESTS[0]: 3})
    auto = bisect_run(trace, run, None, suite_runner=suite, executor_factory=factory)
    assert [c.bisect.decisive_step for c in auto.culprits] == [3]


# --------------------------------------------------------------------------- bring-your-own-repo


def test_auto_oracle_caps_the_number_of_bisected_tests(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=6, break_at=2)
    nodes = [f"tests/t.py::test_{i:02d}" for i in range(MAX_AUTO_CULPRITS + 3)]
    index = {trace.meta.base_snapshot: 0} | {s.snapshot: s.index for s in trace.steps}

    def suite(sha: str) -> tuple[OracleRun, str]:
        failed = index[sha] >= 3
        out = _outcomes({n: "FAILED" if failed else "PASSED" for n in reversed(nodes)})
        return run_of(1 if failed else 0), out

    def factory(check: str):
        return lambda sha: run_of(1 if index[sha] >= 3 else 0)

    report = bisect_run(trace, run, None, suite_runner=suite, executor_factory=factory)
    assert len(report.culprits) == MAX_AUTO_CULPRITS
    assert [c.check for c in report.culprits] == [f"{SUITE_CMD} {n}" for n in nodes[:10]]
    assert report.checks[:10] == [c.check for c in report.culprits]
    assert report.checks[10:] == [f"{SUITE_CMD} {n}{CAP_NOTE}" for n in nodes[10:]]


def test_missing_project_venv_is_not_bisectable(tmp_path: Path) -> None:
    run = tmp_path / "run"
    trace = make_synthetic_run(run, n_steps=4, break_at=2)
    gone = tmp_path / "venv" / "bin" / "python"
    trace.meta.python = gone
    for check in (None, CHECK):
        report = bisect_run(trace, run, check)
        (culprit,) = report.culprits
        assert culprit.bisect.status is BisectStatus.NOT_BISECTABLE
        assert culprit.bisect.reason == f"project venv missing at {gone}"
    result = bisect(trace, run, OracleConfig(check=CHECK))
    assert result.reason == f"project venv missing at {gone}"
