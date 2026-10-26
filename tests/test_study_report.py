"""The study page and RESULTS.draft.md, built from a hand-made StudySummary.

Nothing here depends on agentbisect/study.py: the summary is constructed directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentbisect.models import Metric, RepairEpisode, StudyRow, StudySummary
from agentbisect.report.study import (
    build_context,
    count_rows,
    metric_interval,
    metric_value,
    render_results_draft,
    render_study,
    results_markdown,
)

EVIL = '<script>alert("x")</script>'


def make_summary() -> StudySummary:
    rows = [
        StudyRow(
            run_id="t01-flash-1",
            task_id="t01",
            model="flash",
            n_steps=21,
            final_failed=False,
            bisectable=False,
            total_tokens=460_000,
        ),
        StudyRow(
            run_id="t04-flash-1",
            task_id="t04",
            model="flash",
            n_steps=30,
            final_failed=True,
            bisectable=True,
            check="python -m pytest -q tests/test_report.py::test_csv",
            t_err=9,
            t_obs=14,
            lag=5,
            censored=False,
            steps_after_err=21,
            wasted_tokens=210_000,
            total_tokens=500_000,
        ),
        StudyRow(
            run_id="t08-flash-1",
            task_id="t08",
            model="flash",
            n_steps=25,
            final_failed=True,
            bisectable=True,
            check="python -m pytest -q tests/test_csv.py::test_plain",
            t_err=12,
            censored=True,
            steps_after_err=13,
            wasted_tokens=150_000,
            total_tokens=400_000,
        ),
        StudyRow(
            run_id="s01-flash-1",
            task_id="s01",
            model="flash",
            sabotage_step=7,
            n_steps=20,
            final_failed=True,
            bisectable=True,
            check="python -m pytest -q tests/test_a.py::test_x",
            t_err=7,
            t_obs=7,
            lag=0,
            censored=False,
            steps_after_err=13,
            wasted_tokens=50_000,
            total_tokens=300_000,
            sabotage_correct=True,
        ),
    ]
    episodes = [
        RepairEpisode(
            run_id="t01-flash-1",
            check="python -m pytest -q tests/test_a.py::test_y",
            seen_at=11,
            t_err=8,
            lag=3,
            wasted_tokens=40_000,
        ),
        RepairEpisode(
            run_id="t08-flash-1",
            check="python -m pytest -q tests/test_b.py::test_z",
            seen_at=15,
            reason="oracle error at step 0",
        ),
    ]
    metrics = [
        Metric(
            name="broken-run share", value=0.75, ci_low=0.3, ci_high=0.95, n=4, method="Wilson 95%"
        ),
        Metric(
            name="wasted-token share", value=0.31, ci_low=0.2, ci_high=0.4, n=3, method="bootstrap"
        ),
        Metric(
            name="median lag (steps)", value=None, n=2, method="Kaplan-Meier", note="not reached"
        ),
        Metric(
            name="sabotage accuracy", value=1.0, ci_low=0.44, ci_high=1.0, n=3, method="Wilson 95%"
        ),
        Metric(name="wrong-step rate, flip 0.1", value=0.011, n=3000, method="simulation, seeded"),
    ]
    return StudySummary(
        rows=rows,
        episodes=episodes,
        metrics=metrics,
        excluded={"t04-flash-0": "Alan resumed an earlier conversation"},
        design="Variant B: flash only",
    )


@pytest.fixture
def page(tmp_path: Path) -> str:
    return render_study(make_summary(), tmp_path).read_text()


# --------------------------------------------------------------------------- page


def test_writes_one_static_file(tmp_path: Path) -> None:
    out = render_study(make_summary(), tmp_path / "study")
    assert out.name == "study.html" and out.exists()


def test_page_works_offline(page: str) -> None:
    assert "http://" not in page and "https://" not in page
    assert not re.search(r"<script[^>]+src=", page)
    assert not re.search(r"<link[^>]+href=", page)
    assert "@import" not in page


def test_every_metric_shown_with_value_ci_n_and_method(page: str) -> None:
    for m in make_summary().metrics:
        assert m.name in page
        assert m.method in page
        assert metric_value(m) in page
        interval = metric_interval(m)
        if interval:
            assert interval in page
    assert "75%" in page and "95% CI [30%, 95%]" in page
    assert "not estimable" in page  # KM median not reached


def test_simulation_numbers_are_labelled(page: str) -> None:
    assert "simulation, not real agent runs" in page


def test_runs_table_has_every_row_and_links_to_run_reports(page: str) -> None:
    for rid in ("t01-flash-1", "t04-flash-1", "t08-flash-1", "s01-flash-1"):
        assert f'href="../{rid}/report.html"' in page
    assert 'href="/' not in page  # relative links only
    assert "yes" in page and "censored (never seen, 13 steps)" in page


def test_links_only_to_runs_that_have_a_report(tmp_path: Path) -> None:
    out = render_study(make_summary(), tmp_path, linkable={"t04-flash-1"})
    text = out.read_text()
    assert 'href="../t04-flash-1/report.html"' in text
    assert "../t01-flash-1/report.html" not in text


def test_censored_run_is_drawn_differently(page: str) -> None:
    assert page.count('class="openbar"') == 1  # t08 only
    assert page.count('class="arrow"') == 1
    assert "censored, never seen (n=25)" in page
    assert page.count('class="span"') >= 1  # the observed runs get a solid bar


def test_chart_has_one_row_per_broken_run(page: str) -> None:
    ctx = build_context(make_summary())
    assert ctx["lag_chart"] is not None
    assert [b.kind for b in ctx["lag_chart"].bars] == ["observed", "censored", "instant"]


def test_repaired_breaks_are_a_separate_labelled_panel(page: str) -> None:
    assert "Repaired breaks (a separate series)" in page
    assert "Never pool this series" in page
    assert "oracle error at step 0" in page  # the unfound episode is listed, not drawn
    ctx = build_context(make_summary())
    assert len(ctx["episode_chart"].bars) == 1  # only the bisected episode is drawn


def test_exclusions_listed_with_reasons(page: str) -> None:
    assert "t04-flash-0" in page and "Alan resumed an earlier conversation" in page


def test_claims_box_states_the_limits(page: str) -> None:
    assert "What this can and cannot claim" in page
    assert "this fixture library" in page
    assert "flash in Alan Code" in page
    assert "Nothing about model size" in page
    assert "did not (3 bisectable, 2 observed)" in page  # threshold not met


def test_untrusted_text_is_escaped(tmp_path: Path) -> None:
    s = make_summary()
    s.rows[1].run_id = f"r{EVIL}"
    s.rows[1].task_id = EVIL
    s.rows[1].check = f"pytest tests/t.py::{EVIL}"
    s.rows[1].model = EVIL
    s.excluded[f"x{EVIL}"] = EVIL
    s.episodes[0].reason = EVIL
    s.episodes[0].check = f"pytest tests/t.py::{EVIL}"
    s.metrics[0].note = EVIL
    s.design = EVIL
    text = render_study(s, tmp_path).read_text()
    assert EVIL not in text
    assert "&lt;script&gt;" in text


def test_empty_summary_renders(tmp_path: Path) -> None:
    text = render_study(StudySummary(), tmp_path).read_text()
    assert "No runs." in text and "No metrics were computed." in text
    assert "nothing to draw" in text


def test_only_green_runs_renders_without_chart(tmp_path: Path) -> None:
    s = make_summary()
    s.rows = [s.rows[0]]
    s.episodes = []
    text = render_study(s, tmp_path).read_text()
    assert "nothing to draw" in text and 'class="openbar"' not in text


def test_threshold_met_is_stated() -> None:
    rows = [
        StudyRow(
            run_id=f"r{i}",
            task_id=f"t{i}",
            model="flash",
            n_steps=10,
            final_failed=True,
            bisectable=True,
            check="pytest x::y",
            t_err=2,
            t_obs=4 if i < 3 else None,
            censored=i >= 3,
            lag=2 if i < 3 else None,
        )
        for i in range(6)
    ]
    c = count_rows(rows)
    assert c.pooled_ok and c.observed_broken_runs == 3


# --------------------------------------------------------------------------- draft


def test_results_draft_has_every_metric_and_the_audited_pointer(tmp_path: Path) -> None:
    s = make_summary()
    path = render_results_draft(s, tmp_path)
    assert path.name == "RESULTS.draft.md"
    text = path.read_text()
    assert "Draft generated by `agentbisect study`; the audited version is docs/RESULTS.md." in text
    assert "pending" not in text
    for m in s.metrics:
        assert m.name in text
        assert m.method in text
    assert "not estimable" in text
    assert "95% CI [30%, 95%]" in text
    assert "simulation, not real agent runs" in text
    assert "http://" not in text and "https://" not in text


def test_results_draft_lists_runs_episodes_exclusions_and_claims() -> None:
    text = results_markdown(make_summary())
    for needle in (
        "t04-flash-1",
        "step 9 broke test_csv",
        "a lag of 5 steps",
        "never saw it in the 13 steps after (censored)",
        "oracle error at step 0",
        "t04-flash-0: Alan resumed an earlier conversation",
        "1 of 1 sabotage runs",
        "What this can and cannot claim",
        "Nothing about model size",
    ):
        assert needle in text, needle


def test_results_draft_with_no_runs() -> None:
    text = results_markdown(StudySummary())
    assert "No run ended broken." in text and "No metrics were computed." in text
    assert "the audited version is docs/RESULTS.md." in text


def test_metric_formatting() -> None:
    share = Metric(name="censoring rate", value=0.5, ci_low=0.1, ci_high=0.9, n=4, method="Wilson")
    assert metric_value(share) == "50%"
    assert metric_interval(share) == "95% CI [10%, 90%]"
    lag = Metric(name="median lag", value=3.0, ci_low=1.0, ci_high=6.5, n=5, method="KM")
    assert metric_value(lag) == "3"
    assert metric_interval(lag) == "95% CI [1, 6.5]"
    assert metric_value(Metric(name="x", value=None, n=0, method="m")) == "not estimable"


# --------------------------------------------------------------------------- labels and verdicts


def _row(run_id: str, task: str = "t01", **kw: object) -> StudyRow:
    base = dict(
        run_id=run_id, task_id=task, model="flash", n_steps=10, final_failed=False,
        bisectable=False,
    )  # fmt: skip
    return StudyRow(**{**base, **kw})  # type: ignore[arg-type]


def test_run_labels_are_short_and_unique() -> None:
    from agentbisect.report.study import run_labels

    rows = [
        _row("t01-flash-20261026-095102"),
        _row("t01-flash-20261026-095155"),  # same minute: gets a counter
        _row("t06-flash-20261026-132352", "t06"),
        _row("caught-and-fixed", "s03"),
        _row("humanize-planted-break", "humanize"),
        _row("t01-flash-20261026-095102"),  # a second row of the same run
    ]
    assert run_labels(rows) == {
        "t01-flash-20261026-095102": "t01-0951",
        "t01-flash-20261026-095155": "t01-0951-2",
        "t06-flash-20261026-132352": "t06-1323",
        "caught-and-fixed": "s03",
        "humanize-planted-break": "humanize",
    }


def test_rows_with_one_run_and_one_decisive_step_collapse() -> None:
    from agentbisect.report.study import table_rows

    broke = dict(final_failed=True, bisectable=True, t_err=8, censored=True, total_tokens=100)
    rows = [
        _row("r1", "humanize", check=f"python -m pytest -q tests/t.py::test_a[{i}]", **broke)
        for i in range(3)
    ]
    rows.append(
        _row("r2", "t04", check="python -m pytest -q tests/t.py::test_x", **{**broke, "t_err": 5})
    )
    rows.append(
        _row("r2", "t04", check="python -m pytest -q tests/t.py::test_y", **{**broke, "t_err": 6})
    )
    out = table_rows(StudySummary(rows=rows))
    assert [(t.run, t.broken, t.t_err) for t in out] == [
        ("humanize", "3 tests", "8"),
        ("t04", "test_x", "5"),
        ("t04", "test_y", "6"),
    ]


def test_sabotage_verdict_uses_the_accuracy_rule() -> None:
    from agentbisect.report.study import sabotage_outcomes, sabotage_text

    caught = _row("caught-and-fixed", "s03", sabotage_step=9)  # ends green, sabotage_correct None
    ep = RepairEpisode(run_id="caught-and-fixed", check="c", seen_at=13, t_err=9, lag=4)
    assert sabotage_text(caught, sabotage_outcomes([caught], [ep])) == "found step 9"
    assert sabotage_text(caught, sabotage_outcomes([caught], [])) == "no break seen"
    assert sabotage_text(_row("t01"), {}) == "-"
    other = ep.model_copy(update={"t_err": 3})
    assert sabotage_text(caught, sabotage_outcomes([caught], [other])) == "missed step 9"


def test_page_and_draft_show_the_same_verdict_and_repo_label(tmp_path: Path) -> None:
    caught = _row("caught-and-fixed", "s03", sabotage_step=9)
    own = _row(
        "humanize-planted-break", "humanize", sabotage_step=8, final_failed=True,
        bisectable=True, check="python -m pytest -q tests/t.py::test_a", t_err=8, censored=True,
        sabotage_correct=True,
    )  # fmt: skip
    ep = RepairEpisode(
        run_id="caught-and-fixed", check="python -m pytest -q t.py::t", seen_at=13, t_err=9, lag=4
    )
    s = StudySummary(rows=[caught, own], episodes=[ep])
    html = render_study(s, tmp_path).read_text()
    text = results_markdown(s)
    for page in (html, text):
        assert "found step 9" in page and "found step 8" in page
        assert "humanize" in page and "unknown" not in page
        assert "no result" not in page


def test_writing_twice_leaves_bytes_and_mtime_alone(tmp_path: Path) -> None:
    s = make_summary()
    html = render_study(s, tmp_path)
    draft = render_results_draft(s, tmp_path)
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (html, draft)]
    render_study(s, tmp_path)
    render_results_draft(s, tmp_path)
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in (html, draft)]
