"""Study tests: a temp runs dir of labeled synthetic runs, checked against hand-computed numbers.

The runs are synthetic traces whose ``meta.json`` says ``source=alan_code`` and ``model=flash``, so
the study treats them as Alan Code runs. Every step costs 100 + 50 = 150 tokens.
"""

from __future__ import annotations

import fcntl
import json
import os
import statistics
from collections.abc import Callable
from pathlib import Path

import pytest

from agentbisect import stats
from agentbisect.bisect import SUITE_CMD, _covers, _window, find_episodes, pytest_run
from agentbisect.models import (
    BisectResult,
    BisectStatus,
    CulpritReport,
    RepairEpisode,
    RunReport,
    Step,
    StudyRow,
    StudySummary,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)
from agentbisect.study import collect, summarize, write
from agentbisect.synthetic import OBSERVE_CMD, make_synthetic_run, pytest_failure_output

PER_STEP = 150
FULL_SUITE = "python -m pytest -q"
Mutate = Callable[[Trace], None]


def make_run(
    runs: Path,
    name: str,
    *,
    task: str,
    model: str | None = "flash",
    source: TraceSource = TraceSource.ALAN_CODE,
    finished: bool = True,
    sabotage_step: int | None = None,
    mutate: Mutate | None = None,
    **synthetic: int | None,
) -> Path:
    run_dir = runs / name
    trace = make_synthetic_run(run_dir, **synthetic)  # type: ignore[arg-type]
    trace.meta.source = source
    trace.meta.task_id = task
    trace.meta.model = model
    trace.meta.finished = finished
    trace.meta.sabotage_step = sabotage_step
    if mutate:
        mutate(trace)
    trace.save(run_dir)
    return run_dir


def as_pytest_step(
    trace: Trace, index: int, *, fails_with: int | None, cmd: str | None = None
) -> None:
    """Turn step ``index`` into an agent pytest run that fails (value shown) or passes."""
    step = trace.steps[index - 1]
    step.tool, step.kind = "run_shell", ToolKind.SHELL
    if fails_with is None:
        step.args, step.output, step.exit_code = {"command": cmd or FULL_SUITE}, "2 passed", 0
    else:
        step.args = {"command": cmd or OBSERVE_CMD}
        step.output, step.exit_code = pytest_failure_output(fails_with), 1


def repaired_once(trace: Trace) -> None:
    # step 3 breaks, 4 shows it, 5 shows it again, 7 fixes it, 9 runs the suite and it passes
    as_pytest_step(trace, 5, fails_with=-1)
    as_pytest_step(trace, 9, fails_with=None)


def repaired_twice(trace: Trace) -> None:
    # break 3, seen 4; fix 6; suite passes at 8; break 10 (add returns 6), seen at 11
    as_pytest_step(trace, 8, fails_with=None)
    as_pytest_step(trace, 11, fails_with=6)


def sabotage_fixture(trace: Trace) -> None:
    trace.meta.sabotage_step = 6


def build_runs(runs: Path) -> None:
    # A: broken at 4, seen at 7 (lag 3).  B: broken at 5, never seen (censored).
    make_run(runs, "t02-flash-a", task="t02", n_steps=12, break_at=4, observe_at=7)
    make_run(runs, "t05-flash-b", task="t05", n_steps=10, break_at=5)
    # C: green run (breaks at 3, fixed at 5, nobody ran a test).
    make_run(runs, "t07-flash-c", task="t07", n_steps=8, break_at=3, fix_at=5)
    # D: one repaired break, seen at 4 and again at 5, suite green at 9.
    make_run(
        runs,
        "t08-flash-d",
        task="t08",
        n_steps=12,
        break_at=3,
        fix_at=7,
        observe_at=4,
        mutate=repaired_once,
    )
    # E: two breaks seen; ends broken.
    make_run(
        runs,
        "t09-flash-e",
        task="t09",
        n_steps=14,
        break_at=3,
        fix_at=6,
        observe_at=4,
        break_again_at=10,
        mutate=repaired_twice,
    )
    # Sabotage runs: one where bisect lands on the injected step, one with a wrong ground truth.
    make_run(runs, "s01-flash", task="s01", n_steps=10, break_at=6, mutate=sabotage_fixture)
    make_run(
        runs,
        "s02-flash",
        task="s02",
        n_steps=10,
        break_at=6,
        mutate=lambda t: setattr(t.meta, "sabotage_step", 7),
    )
    # Not study data.
    resumed = make_run(runs, "t04-flash-resumed", task="t04", n_steps=3, break_at=2)
    (resumed / "finalize.json").write_text(json.dumps({"resumed_conversation": True}))
    make_run(runs, "x-old", task="t04", n_steps=3, break_at=2)
    make_run(runs, "t03-fake", task="t03", n_steps=3, break_at=2, source=TraceSource.ALAN)
    make_run(runs, "t03-syn", task="t03", n_steps=3, break_at=2, source=TraceSource.SYNTHETIC)
    make_run(runs, "t06-flash-running", task="t06", n_steps=3, break_at=2, finished=False)
    make_run(runs, "t06-qwen", task="t06", n_steps=3, break_at=2, model="qwen")
    events_only = make_run(runs, "t01-flash-eo", task="t01", n_steps=3, break_at=2)
    (events_only / "finalize.json").write_text(
        json.dumps({"mode": "events_only", "per_step_snapshots": False})
    )
    (runs / "empty").mkdir()
    (runs / "study").mkdir()  # the output dir is never a run


@pytest.fixture(scope="module")
def study_state(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, StudySummary]:
    """Build the runs and collect them once per test session, even under pytest-xdist.

    Bisecting the fixture runs takes about a minute of real pytest calls, so xdist workers share
    one build: the first takes a file lock and does the work, the others read its result.
    """
    base = tmp_path_factory.getbasetemp()
    shared = (base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base) / "study-fixture"
    shared.mkdir(exist_ok=True)
    done = shared / "summary.json"
    runs = shared / "runs"
    with (shared / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not done.exists():
            build_runs(runs)
            done.write_text(collect(runs).model_dump_json())
    return runs, StudySummary.model_validate_json(done.read_text())


@pytest.fixture
def runs_dir(study_state: tuple[Path, StudySummary]) -> Path:
    return study_state[0]


@pytest.fixture
def summary(study_state: tuple[Path, StudySummary]) -> StudySummary:
    return study_state[1].model_copy(deep=True)


def rows_of(summary: StudySummary, run_id: str) -> list[StudyRow]:
    return [r for r in summary.rows if r.run_id == run_id]


# --------------------------------------------------------------------------- exclusions


def test_exclusions_name_the_reason(summary: StudySummary) -> None:
    ex = summary.excluded
    assert set(ex) == {
        "t04-flash-resumed",
        "x-old",
        "t03-fake",
        "t03-syn",
        "t06-flash-running",
        "t06-qwen",
        "t01-flash-eo",
        "empty",
    }
    assert "resumed" in ex["t04-flash-resumed"]
    assert "x-" in ex["x-old"]
    assert "source alan)" in ex["t03-fake"]
    assert "source synthetic" in ex["t03-syn"]
    assert "unfinished" in ex["t06-flash-running"]
    assert "qwen" in ex["t06-qwen"]
    assert "events-only" in ex["t01-flash-eo"]
    assert "meta.json" in ex["empty"]


def test_resumed_marker_in_events_excludes_run(tmp_path: Path) -> None:
    run = make_run(tmp_path, "t04-flash-r", task="t04", n_steps=3, break_at=2)
    (run / "events.jsonl").write_text(
        json.dumps({"type": "message.updated", "properties": {"text": "<resume_context>"}}) + "\n"
    )
    out = collect(tmp_path)
    assert "resumed" in out.excluded["t04-flash-r"]
    assert out.rows == []


# --------------------------------------------------------------------------- rows


def test_kept_runs_are_exactly_the_study_runs(summary: StudySummary) -> None:
    assert {r.run_id for r in summary.rows} == {
        "t02-flash-a",
        "t05-flash-b",
        "t07-flash-c",
        "t08-flash-d",
        "t09-flash-e",
        "s01-flash",
        "s02-flash",
    }


def test_observed_break_row(summary: StudySummary) -> None:
    (row,) = rows_of(summary, "t02-flash-a")
    assert row.task_id == "t02" and row.model == "flash" and row.sabotage_step is None
    assert row.check == "python -m pytest -q tests/test_calc.py::test_add"
    assert (row.final_failed, row.bisectable) == (True, True)
    assert (row.t_err, row.t_obs, row.lag, row.censored) == (4, 7, 3, False)
    assert row.n_steps == 12 and row.total_tokens == 12 * PER_STEP
    assert row.steps_after_err == 8
    assert row.wasted_tokens == 8 * PER_STEP
    assert row.sabotage_correct is None


def test_censored_break_row(summary: StudySummary) -> None:
    (row,) = rows_of(summary, "t05-flash-b")
    assert (row.t_err, row.t_obs, row.lag, row.censored) == (5, None, None, True)
    assert row.steps_after_err == 5
    assert row.wasted_tokens == 5 * PER_STEP and row.total_tokens == 10 * PER_STEP


def test_green_run_has_one_row_without_a_culprit(summary: StudySummary) -> None:
    (row,) = rows_of(summary, "t07-flash-c")
    assert (row.final_failed, row.bisectable, row.check, row.t_err) == (False, False, None, None)
    assert row.censored is None and row.sabotage_correct is None
    assert row.total_tokens == 8 * PER_STEP


def test_sabotage_rows_are_scored_against_the_injected_step(summary: StudySummary) -> None:
    (good,) = rows_of(summary, "s01-flash")
    (bad,) = rows_of(summary, "s02-flash")
    assert (good.sabotage_step, good.t_err, good.sabotage_correct) == (6, 6, True)
    assert (bad.sabotage_step, bad.t_err, bad.sabotage_correct) == (7, 6, False)


def test_run_that_ends_broken_after_a_repair_is_bisected(summary: StudySummary) -> None:
    (row,) = rows_of(summary, "t09-flash-e")
    assert row.final_failed and row.bisectable
    assert row.t_err in (3, 10)  # plain bisect may stop on either transition (METHODS §3.2)
    assert row.censored is False and row.lag == 1


def test_bisect_json_is_written_and_reused(runs_dir: Path, summary: StudySummary) -> None:
    path = runs_dir / "t02-flash-a" / "bisect.json"
    assert path.exists()
    again = collect(runs_dir)  # reads bisect.json and the episode cache
    assert [r.model_dump() for r in again.rows] == [r.model_dump() for r in summary.rows]
    assert [e.model_dump() for e in again.episodes] == [e.model_dump() for e in summary.episodes]


# --------------------------------------------------------------------------- episodes


def episodes_of(summary: StudySummary, run_id: str) -> list[RepairEpisode]:
    return [e for e in summary.episodes if e.run_id == run_id]


def test_episode_in_a_run_that_ends_broken(summary: StudySummary) -> None:
    (ep,) = episodes_of(summary, "t02-flash-a")
    assert (ep.seen_at, ep.t_err, ep.lag) == (7, 4, 3)
    assert ep.wasted_tokens == 3 * PER_STEP  # steps 5..7
    assert ep.check == "python -m pytest -q tests/test_calc.py::test_add"
    assert ep.reason is None


def test_repeated_failures_make_one_episode(summary: StudySummary) -> None:
    # D: the test fails at pytest steps 4 and 5 with no pass between: one episode, at step 4.
    (ep,) = episodes_of(summary, "t08-flash-d")
    assert (ep.seen_at, ep.t_err, ep.lag, ep.wasted_tokens) == (4, 3, 1, PER_STEP)


def test_second_break_after_a_pass_starts_a_new_episode(summary: StudySummary) -> None:
    first, second = episodes_of(summary, "t09-flash-e")
    assert (first.seen_at, first.t_err, first.lag, first.wasted_tokens) == (4, 3, 1, PER_STEP)
    # The agent saw the test pass at step 8, so the second episode searches steps 9..11 only and
    # finds the second break (step 10), not the old one.
    assert (second.seen_at, second.t_err, second.lag, second.wasted_tokens) == (11, 10, 1, PER_STEP)


def test_no_pytest_failure_no_episode(summary: StudySummary) -> None:
    for run_id in ("t05-flash-b", "t07-flash-c", "s01-flash", "s02-flash"):
        assert episodes_of(summary, run_id) == []
    assert len(summary.episodes) == 4


def test_unbisectable_episode_keeps_its_reason(tmp_path: Path) -> None:
    # The output claims test_add failed at step 4, but the repo at step 4 is fine: the
    # precondition (fails at k) does not hold.
    def lie(trace: Trace) -> None:
        as_pytest_step(trace, 4, fails_with=-1)

    run = make_run(tmp_path, "t01-flash-lie", task="t01", n_steps=8, break_at=6, mutate=lie)
    (ep,) = find_episodes(Trace.load(run), run)
    assert ep.seen_at == 4 and ep.t_err is None and ep.lag is None
    assert ep.reason and "does not fail" in ep.reason


def test_sabotage_run_still_has_episodes_but_the_lag_series_drops_them(tmp_path: Path) -> None:
    def seen(trace: Trace) -> None:
        as_pytest_step(trace, 5, fails_with=-1)

    run = make_run(
        tmp_path, "s03-flash", task="s03", n_steps=8, break_at=3, sabotage_step=3, mutate=seen
    )
    (ep,) = find_episodes(Trace.load(run), run)
    assert (ep.t_err, ep.seen_at, ep.lag) == (3, 5, 2)
    summary = summarize(collect(tmp_path))
    assert [e.t_err for e in summary.episodes] == [3]
    assert metric(summary, "Repaired-break episodes").value == 0


def test_sabotage_accuracy_counts_a_seen_break_in_a_run_that_ends_green() -> None:
    def sab_row(run_id: str) -> StudyRow:
        return StudyRow(
            run_id=run_id,
            task_id="s03",
            model="flash",
            sabotage_step=4,
            n_steps=9,
            final_failed=False,
            bisectable=False,
        )

    eps = [
        RepairEpisode(run_id="s1", check="c", seen_at=7, t_err=4, lag=3),  # the injected step
        RepairEpisode(run_id="s2", check="c", seen_at=7, t_err=6, lag=1),  # another step
    ]
    rows = [sab_row("s1"), sab_row("s2"), sab_row("s3")]  # s3: green, nothing seen: not rated
    s = summarize(StudySummary(rows=rows, episodes=eps))
    sab = metric(s, "Sabotage accuracy")
    assert (sab.value, sab.n) == (0.5, 2)


def test_episodes_of_sabotage_runs_are_not_counted() -> None:
    eps = [
        RepairEpisode(run_id="t1", check="c", seen_at=5, t_err=3, lag=2),
        RepairEpisode(run_id="s1", check="c", seen_at=5, t_err=2, lag=3),
    ]
    rows = [
        StudyRow(
            run_id="t1",
            task_id="t1",
            model="flash",
            n_steps=9,
            final_failed=False,
            bisectable=False,
        ),
        StudyRow(
            run_id="s1",
            task_id="s1",
            model="flash",
            sabotage_step=2,
            n_steps=9,
            final_failed=True,
            bisectable=True,
            check="c",
            t_err=2,
            sabotage_correct=True,
        ),
    ]
    s = summarize(StudySummary(rows=rows, episodes=eps))
    assert metric(s, "Repaired-break episodes").value == 1
    assert metric(s, "Repaired-break lag, median (steps)").value == 2.0


def test_suite_that_cannot_run_at_step_0_excludes_the_run(tmp_path: Path) -> None:
    def report(reason: str) -> RunReport:
        failed = BisectResult(check=SUITE_CMD, status=BisectStatus.NOT_BISECTABLE, reason=reason)
        return RunReport(
            run_id="r",
            checks=[SUITE_CMD],
            culprits=[CulpritReport(run_id="r", check=SUITE_CMD, bisect=failed)],
            auto_oracle=True,
        )

    step0 = report("full suite could not run at step 0: check timed out after 120s")
    stepn = report("full suite could not run at the final step 3: check timed out after 120s")
    for name, rep in (("t01-flash-env", step0), ("t01-flash-hang", stepn)):
        run = make_run(tmp_path, name, task="t01", n_steps=3, break_at=2)
        (run / "bisect.json").write_text(rep.model_dump_json())
    out = collect(tmp_path)
    assert "suite cannot run at step 0" in out.excluded["t01-flash-env"]
    assert rows_of(out, "t01-flash-env") == []
    (row,) = rows_of(out, "t01-flash-hang")  # only at step n: the agent broke it
    assert (row.final_failed, row.bisectable) == (True, False)
    assert "t01-flash-hang" not in out.excluded


# --------------------------------------------------------------------------- helpers


def test_pytest_run_reads_path_arguments() -> None:
    assert pytest_run("ls -R") is None
    assert pytest_run("python -m pytest -q") == ([], False)
    assert pytest_run("'/v/bin/python3' -m pytest -q tests/test_a.py::test_x && ruff check .") == (
        ["tests/test_a.py::test_x"],
        False,
    )
    assert pytest_run("pytest --tb short -x tests/") == (["tests/"], False)
    assert pytest_run("pytest tests/test_a.py -k foo")[1] is True
    assert pytest_run("pytest --lf")[1] is True


def test_covers() -> None:
    node = "tests/test_a.py::TestC::test_f[1]"
    assert _covers([], False, node)
    assert _covers(["tests"], False, node) and _covers(["./tests/"], False, node)
    assert _covers(["tests/test_a.py"], False, node)
    assert _covers(["tests/test_a.py::TestC"], False, node)
    assert _covers(["tests/test_a.py::TestC::test_f"], False, node)
    assert not _covers(["tests/test_b.py"], False, node)
    assert not _covers(["tests/test_a.py::TestC::test_g"], False, node)
    assert not _covers([], True, node)


def test_window_shifts_indices_and_base(tmp_path: Path) -> None:
    trace = make_synthetic_run(tmp_path / "w", n_steps=6, break_at=3)
    w = _window(trace, 2, 5)
    assert [s.index for s in w.steps] == [1, 2, 3]
    assert w.meta.base_snapshot == trace.snapshot_at(2)
    assert w.snapshot_at(3) == trace.snapshot_at(5) and w.n == 3
    assert _window(trace, 0, 4).n == 4 and _window(trace, 0, 4).meta.base_snapshot == (
        trace.meta.base_snapshot
    )


# --------------------------------------------------------------------------- metrics


def metric(summary: StudySummary, name: str):
    (m,) = [m for m in summary.metrics if m.name == name]
    return m


def test_metrics_match_hand_computation(summary: StudySummary) -> None:
    s = summarize(summary)
    e = rows_of(s, "t09-flash-e")[0]

    runs = metric(s, "Runs")
    assert (runs.value, runs.n) == (7, 7)

    broken = metric(s, "Broken-run share")  # A, B, E of 5 task runs
    assert (broken.value, broken.n) == (3 / 5, 5)
    assert (broken.ci_low, broken.ci_high) == stats.wilson_interval(3, 5)

    found = metric(s, "Bisectable share of broken runs")
    assert (found.value, found.n) == (1.0, 3)
    assert (found.ci_low, found.ci_high) == stats.wilson_interval(3, 3)

    cens = metric(s, "Censoring rate of broken runs")  # only B is censored
    assert (cens.value, cens.n) == (1 / 3, 3)
    assert (cens.ci_low, cens.ci_high) == stats.wilson_interval(1, 3)

    once = metric(s, "Noticed at once (lag 0)")  # lags 3, censored, 1: nobody at lag 0
    assert (once.value, once.n) == (0.0, 3)
    assert (once.ci_low, once.ci_high) == stats.wilson_interval(0, 3)

    km = metric(s, "Detection lag, Kaplan-Meier median (steps)")
    assert km.value is None and km.n == 3 and "needs at least 6" in (km.note or "")

    total = 12 * PER_STEP + 10 * PER_STEP + 14 * PER_STEP
    wasted = 8 * PER_STEP + 5 * PER_STEP + (e.wasted_tokens or 0)
    pooled = metric(s, "Wasted-token share (pooled)")
    assert pooled.value == pytest.approx(wasted / total) and pooled.n == 3
    assert pooled.ci_low is not None and pooled.ci_high is not None
    assert pooled.ci_low <= pooled.value <= pooled.ci_high
    shares = [8 / 12, 5 / 10, (e.wasted_tokens or 0) / (14 * PER_STEP)]
    assert metric(s, "Wasted-token share (median per run)").value == pytest.approx(
        statistics.median(shares)
    )

    sab = metric(s, "Sabotage accuracy")  # s01 right, s02 wrong
    assert (sab.value, sab.n) == (0.5, 2)
    assert (sab.ci_low, sab.ci_high) == stats.wilson_interval(1, 2)

    assert metric(s, "Repaired-break episodes").value == 4
    lag = metric(s, "Repaired-break lag, median (steps)")  # lags 3, 1, 1, 1
    assert (lag.value, lag.n) == (1.0, 4)
    expected = stats.bootstrap_ci(
        [[3.0], [1.0], [1.0, 1.0]], lambda rs: statistics.median([x for r in rs for x in r])
    )
    assert (lag.ci_low, lag.ci_high) == expected
    assert all(m.method and m.n >= 0 for m in s.metrics)
    assert "variant B" in s.design


def test_wasted_share_bootstraps_tasks_not_runs() -> None:
    def row(run: str, task: str, wasted: int, total: int) -> StudyRow:
        return StudyRow(
            run_id=run,
            task_id=task,
            model="flash",
            n_steps=10,
            final_failed=True,
            bisectable=True,
            check="c",
            t_err=2,
            censored=True,
            steps_after_err=8,
            wasted_tokens=wasted,
            total_tokens=total,
        )

    rows = [row("a1", "t1", 10, 100), row("a2", "t1", 30, 100), row("b1", "t2", 50, 100)]
    s = summarize(StudySummary(rows=rows))
    m = metric(s, "Wasted-token share (pooled)")
    assert m.value == pytest.approx(90 / 300)
    groups = [[rows[0], rows[1]], [rows[2]]]
    lo, hi = stats.bootstrap_ci(
        groups,
        lambda gs: (
            sum(r.wasted_tokens for g in gs for r in g) / sum(r.total_tokens for g in gs for r in g)
        ),
    )
    assert (m.ci_low, m.ci_high) == (lo, hi)
    assert "2 task" in (m.note or "")


def test_km_median_is_reported_once_the_threshold_is_met() -> None:
    def row(i: int, lag: int | None) -> StudyRow:
        cens = lag is None
        return StudyRow(
            run_id=f"r{i}",
            task_id=f"t{i}",
            model="flash",
            n_steps=20,
            final_failed=True,
            bisectable=True,
            check="c",
            t_err=5,
            t_obs=None if cens else 5 + lag,
            lag=lag,
            censored=cens,
            steps_after_err=15,
            wasted_tokens=100,
            total_tokens=1000,
        )

    # Durations 1,2,2,3,15,15 with the last two censored: S(1) = 5/6, S(2) = 5/6 * 3/5 = 0.5,
    # so the median is 2.
    rows = [row(1, 1), row(2, 2), row(3, 2), row(4, 3), row(5, None), row(6, None)]
    m = metric(summarize(StudySummary(rows=rows)), "Detection lag, Kaplan-Meier median (steps)")
    ref = stats.km_median_ci([1, 2, 2, 3, 15, 15], [True, True, True, True, False, False])
    assert m.value == ref.median == 2.0
    assert (m.ci_low, m.ci_high) == (ref.lower, ref.upper)
    assert m.n == 6

    # Six runs but only two observed: below the threshold, so no number.
    few = [row(1, 1), row(2, 2)] + [row(i, None) for i in range(3, 7)]
    assert (
        metric(
            summarize(StudySummary(rows=few)), "Detection lag, Kaplan-Meier median (steps)"
        ).value
        is None
    )


def test_primary_row_per_run_is_the_earliest_culprit() -> None:
    def row(check: str, t_err: int, lag: int | None) -> StudyRow:
        return StudyRow(
            run_id="r",
            task_id="t",
            model="flash",
            n_steps=20,
            final_failed=True,
            bisectable=True,
            check=check,
            t_err=t_err,
            t_obs=None if lag is None else t_err + lag,
            lag=lag,
            censored=lag is None,
            steps_after_err=20 - t_err,
            wasted_tokens=10 * (20 - t_err),
            total_tokens=200,
        )

    rows = [row("b", 9, 0), row("a", 4, None), row("c", 4, 2)]
    m = metric(summarize(StudySummary(rows=rows)), "Censoring rate of broken runs")
    # t_err ties at 4; the observed one (c, lag 2) wins over the censored one, so 0 of 1 censored.
    assert (m.value, m.n) == (0.0, 1)
    pooled = metric(summarize(StudySummary(rows=rows)), "Wasted-token share (pooled)")
    assert pooled.value == pytest.approx(160 / 200)


def test_empty_study_has_no_numbers_but_does_not_crash() -> None:
    s = summarize(StudySummary())
    assert {m.name for m in s.metrics} >= {"Runs", "Broken-run share", "Sabotage accuracy"}
    assert metric(s, "Runs").value == 0
    assert metric(s, "Broken-run share").value is None


# --------------------------------------------------------------------------- write


def test_write_round_trips(tmp_path: Path, summary: StudySummary) -> None:
    s = summarize(summary)
    path = write(s, tmp_path / "out")
    assert path == tmp_path / "out" / "study.json"
    back = StudySummary.model_validate_json(path.read_text())
    assert back.rows == s.rows and back.episodes == s.episodes
    assert [m.name for m in back.metrics] == [m.name for m in s.metrics]
    assert back.excluded == s.excluded


def test_collect_never_touches_the_excluded_runs(runs_dir: Path) -> None:
    untouched = runs_dir / "x-old"
    assert not (untouched / "bisect.json").exists()
    assert not (runs_dir / "t03-syn" / "bisect.json").exists()


# --------------------------------------------------------------------------- no opportunity (§4.4)


def test_no_opportunity_rows_leave_the_lag_statistics_but_not_the_shares() -> None:
    def row(run: str, t_err: int, lag: int | None, no_opp: bool = False, **kw) -> StudyRow:
        return StudyRow(
            run_id=run,
            task_id="t",
            model="flash",
            n_steps=20,
            final_failed=True,
            bisectable=True,
            check="c",
            t_err=t_err,
            t_obs=None if lag is None else t_err + lag,
            lag=lag,
            censored=lag is None,
            steps_after_err=20 - t_err,
            wasted_tokens=10 * (20 - t_err),
            total_tokens=200,
            no_opportunity=no_opp,
            **kw,
        )

    rows = [
        row("a", 10, 2),
        row("b", 12, None),
        row("c", 20, None, no_opp=True),
        row("s", 20, None, no_opp=True, sabotage_step=20, sabotage_correct=True),
    ]
    s = summarize(StudySummary(rows=rows))
    assert (metric(s, "Broken-run share").value, metric(s, "Broken-run share").n) == (1.0, 3)
    assert metric(s, "Bisectable share of broken runs").n == 3
    cens = metric(s, "Censoring rate of broken runs")  # only a and b: b is censored
    assert (cens.value, cens.n) == (0.5, 2)
    assert metric(s, "Noticed at once (lag 0)").n == 2
    assert metric(s, "Wasted-token share (pooled)").n == 2
    assert metric(s, "Wasted-token share (pooled)").value == pytest.approx((100 + 80) / 400)
    count = metric(s, "No opportunity to observe (break on last step)")
    assert (count.value, count.n) == (1.0, 3) and "Sabotage runs with this flag: 1" in count.note
    sab = metric(s, "Sabotage accuracy")  # the no-opportunity sabotage row still counts
    assert (sab.value, sab.n) == (1.0, 1)


def test_piped_pytest_failure_starts_an_episode_and_a_cut_off_tail_is_not_a_pass() -> None:
    from agentbisect.bisect import _could_show_failure, _episode_starts, _shows_pass

    def sh(i: int, cmd: str, out: str) -> Step:
        return Step(
            index=i, tool="bash", kind=ToolKind.SHELL, args={"command": cmd}, output=out,
            exit_code=0,
        )  # fmt: skip

    node = "tests/test_calc.py::test_add"
    fail = sh(1, "python -m pytest -q | tail -3", f"FAILED {node} - assert 1 == 2\n1 failed in 1s")
    cut = sh(2, "python -m pytest -q | tail -3", "...\n  some tail text")
    ok = sh(3, "python -m pytest -q | tail -3", "4 passed in 0.1s")
    assert _could_show_failure(fail) and not _could_show_failure(sh(4, "ls", "FAILED"))
    assert not _shows_pass(cut) and _shows_pass(ok)
    assert _shows_pass(sh(5, "python -m pytest -q", "no summary"))  # unpiped: exit code rules
    trace = Trace(
        meta=TraceMeta(
            run_id="r", source=TraceSource.ALAN_CODE, workdir=Path("/w"), base_snapshot="0" * 40
        ),
        steps=[fail, cut, ok],
    )
    (start,) = _episode_starts(trace, [node])
    assert (start.k, start.fixed) == (1, True)  # fixed only by the step that shows "4 passed"
    only_cut = Trace(meta=trace.meta, steps=[fail, cut])
    (start,) = _episode_starts(only_cut, [node])
    assert not start.fixed


def test_own_repo_run_without_planted_break_is_a_case_study(tmp_path):
    from agentbisect.models import Step, ToolKind, Trace, TraceMeta, TraceSource
    from agentbisect.study import _load_study_run

    run = tmp_path / "proj-run"
    meta = TraceMeta(
        run_id="proj-run", source=TraceSource.ALAN_CODE, workdir=tmp_path, base_snapshot="0" * 40,
        model="flash", finished=True, repo_origin="https://example.com/p.git",
    )  # fmt: skip
    Trace(meta=meta, steps=[Step(index=1, tool="read", kind=ToolKind.READ)]).save(run)
    (run / "finalize.json").write_text('{"mode": "hooks", "per_step_snapshots": true}')
    trace, reason = _load_study_run(run)
    assert trace is None and "case study" in reason


# --------------------------------------------------------------------------- labels and replays


def test_repo_name_from_origin() -> None:
    from agentbisect.study import repo_name

    assert repo_name("https://github.com/python-humanize/humanize") == "humanize"
    assert repo_name("https://github.com/python-humanize/humanize.git/") == "humanize"
    assert repo_name("git@github.com:me/proj.git") == "proj"
    assert repo_name("/Users/me/code/proj") == "proj"
    assert repo_name(None) is None and repo_name("") is None


def test_own_repo_rows_carry_the_repo_name_not_unknown(tmp_path: Path) -> None:
    from agentbisect.study import _rows

    trace = make_synthetic_run(tmp_path / "r", n_steps=5, break_at=3)
    trace.meta.task_id = None
    trace.meta.repo_origin = "https://github.com/python-humanize/humanize"
    rows = _rows("humanize-planted-break", trace, RunReport(run_id="r", checks=[]))
    assert [r.task_id for r in rows] == ["humanize"]
    trace.meta.repo_origin = None
    assert _rows("x", trace, RunReport(run_id="r", checks=[]))[0].task_id == "unknown"


def test_sabotage_outcomes_follow_the_accuracy_rule() -> None:
    from agentbisect.report.study import sabotage_outcomes

    def row(run_id: str, **kw: object) -> StudyRow:
        base = dict(
            run_id=run_id, task_id="s", model="flash", sabotage_step=4, n_steps=9,
            final_failed=False, bisectable=False,
        )  # fmt: skip
        return StudyRow(**{**base, **kw})  # type: ignore[arg-type]

    rows = [
        row("fixed"),  # ends green, break seen and fixed at the planted step
        row("wrong"),  # ends green, seen break at another step
        row("quiet"),  # ends green, nothing seen
        row("final-ok", final_failed=True, bisectable=True, check="c", sabotage_correct=True),
        row("final-bad", final_failed=True, bisectable=True, check="c", sabotage_correct=False),
        row("no-step", final_failed=True, bisectable=False, check="c", sabotage_correct=False),
    ]
    eps = [
        RepairEpisode(run_id="fixed", check="c", seen_at=7, t_err=4, lag=3),
        RepairEpisode(run_id="wrong", check="c", seen_at=7, t_err=6, lag=1),
    ]
    assert sabotage_outcomes(rows, eps) == {
        "fixed": True,
        "wrong": False,
        "quiet": None,
        "final-ok": True,
        "final-bad": False,
        "no-step": False,
    }


def test_replaying_a_study_leaves_every_file_byte_identical(tmp_path: Path) -> None:
    import time
    from datetime import UTC, datetime, timedelta

    from agentbisect.report.study import render_results_draft, render_study
    from agentbisect.study import keep_created_at
    from tests.test_study_report import make_summary

    def build(created: datetime) -> StudySummary:
        return make_summary().model_copy(update={"created_at": created})

    out = tmp_path / "study"
    first = build(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    write(first, out)
    render_study(first, out)
    render_results_draft(first, out)
    names = ("study.json", "study.html", "RESULTS.draft.md")
    before = {n: ((out / n).read_bytes(), (out / n).stat().st_mtime_ns) for n in names}

    time.sleep(0.01)
    again = keep_created_at(build(first.created_at + timedelta(hours=3)), out)
    write(again, out)
    render_study(again, out)
    render_results_draft(again, out)
    for n in names:
        assert (out / n).read_bytes() == before[n][0], n
        assert (out / n).stat().st_mtime_ns == before[n][1], f"{n} was rewritten"

    changed = build(first.created_at + timedelta(hours=3))
    changed.design = "a different design"
    again = keep_created_at(changed, out)
    assert again.created_at == changed.created_at  # a real change keeps its own time
    write(again, out)
    assert "a different design" in (out / "study.json").read_text()
