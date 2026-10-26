"""The study page and RESULTS.draft.md, built from a hand-made StudySummary.

Nothing here depends on agentbisect/study.py: the summary is constructed directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from agentbisect.models import (
    Analysis,
    BisectResult,
    BisectStatus,
    CulpritReport,
    Metric,
    RepairEpisode,
    RunReport,
    StudyRow,
    StudySummary,
)
from agentbisect.report.render import render_report
from agentbisect.report.study import (
    build_context,
    count_rows,
    metric_interval,
    metric_value,
    render_results_draft,
    render_study,
    results_markdown,
)
from agentbisect.synthetic import make_synthetic_run
from tests.test_report import DATA_RE, needs_node, run_page

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
def page_file(tmp_path: Path) -> Path:
    return render_study(make_summary(), tmp_path)


@pytest.fixture
def page(page_file: Path) -> str:
    return page_file.read_text()


# --------------------------------------------------------------------------- page


def data_of(page: str) -> dict[str, Any]:
    m = DATA_RE.search(page)
    assert m, "no data blob"
    return json.loads(m.group(1))


def test_writes_one_static_file(tmp_path: Path) -> None:
    out = render_study(make_summary(), tmp_path / "study")
    assert out.name == "study.html" and out.exists()


def test_page_works_offline(page: str) -> None:
    outside_data = DATA_RE.sub("", page)
    assert "http://" not in outside_data and "https://" not in outside_data
    assert not re.search(r"<script[^>]+src=", page)
    assert not re.search(r"<link[^>]+href=", page)
    assert "@import" not in page and "url(" not in page
    assert len(page) < 500_000


def test_page_is_deterministic(tmp_path: Path) -> None:
    first = render_study(make_summary(), tmp_path / "a").read_bytes()
    second = render_study(make_summary(), tmp_path / "b").read_bytes()
    assert first == second


def test_every_metric_shown_with_value_ci_n_and_method(page: str) -> None:
    data = data_of(page)
    assert [m["name"] for m in data["metrics"]] == [m.name for m in make_summary().metrics]
    for m in make_summary().metrics:
        assert m.name in page  # in the data and in the no-script text
        assert m.method in page
        assert metric_value(m) in page
        interval = metric_interval(m)
        if interval:
            assert interval in page
    by_name = {m["name"]: m for m in data["metrics"]}
    first = by_name["broken-run share"]
    assert first["value"] == "75%" and first["interval"] == "95% CI [30%, 95%]"
    assert (first["raw"], first["low"], first["high"], first["percent"]) == (0.75, 0.3, 0.95, True)
    assert by_name["median lag (steps)"]["value"] == "not estimable"  # KM median not reached
    assert by_name["median lag (steps)"]["estimable"] is False


def test_simulation_numbers_are_labelled(page: str) -> None:
    assert "simulation, not real agent runs" in page
    flags = {m["name"]: m["simulation"] for m in data_of(page)["metrics"]}
    assert flags["wrong-step rate, flip 0.1"] is True and flags["broken-run share"] is False


def test_runs_table_has_every_row_and_links_to_run_reports(page: str) -> None:
    ids = ("t01-flash-1", "t04-flash-1", "t08-flash-1", "s01-flash-1")
    for rid in ids:
        assert f'href="../{rid}/report.html"' in page  # the no-script table
    data = data_of(page)
    assert [r["run_id"] for r in data["runs"]] == list(ids)
    assert all(r["href"] == f"../{r['run_id']}/report.html" for r in data["runs"])
    assert 'href="/' not in page  # relative links only
    lags = {r["run_id"]: r["lag"] for r in data["runs"]}
    assert lags["t08-flash-1"] == "censored (never seen, 13 steps)"
    assert lags["t04-flash-1"] == "5"


def test_last_step_break_is_not_called_never_seen(tmp_path: Path) -> None:
    s = make_summary()
    s.rows[2].no_opportunity = True  # t08: the break came on the last step
    data = data_of(render_study(s, tmp_path).read_text())
    row = next(r for r in data["runs"] if r["run_id"] == "t08-flash-1")
    assert row["lag"] == "last step, no chance to see it" and row["censored"] is True
    assert any(b["text"].startswith("last step") for b in data["lag_chart"]["bars"])


def test_links_only_to_runs_that_have_a_report(tmp_path: Path) -> None:
    out = render_study(make_summary(), tmp_path, linkable={"t04-flash-1"})
    text = out.read_text()
    assert 'href="../t04-flash-1/report.html"' in text
    assert "../t01-flash-1/report.html" not in text
    hrefs = {r["run_id"]: r["href"] for r in data_of(text)["runs"]}
    assert hrefs["t01-flash-1"] is None and hrefs["t04-flash-1"]


def test_censored_run_is_drawn_differently() -> None:
    ctx = build_context(make_summary())
    bars = {b.text: b for b in ctx["lag_chart"].bars}
    open_bars = [b for b in ctx["lag_chart"].bars if b.kind == "censored"]
    assert len(open_bars) == 1 and open_bars[0].t_obs is None  # t08 only
    assert open_bars[0].text == "censored, never seen (n=25)"
    assert bars["lag 5"].kind == "observed" and bars["lag 0"].kind == "instant"


@needs_node
def test_chart_draws_an_open_arrow_for_the_censored_run(page_file: Path) -> None:
    out = run_page(
        page_file,
        """
        const count = (c) => H.byClass(c).length;
        return { open: count("open"), arrow: count("arrowhead"), span: count("span"),
                 rows: count("lrow"), tip: H.byClass("tip").map((t) => t.textContent) };
        """,
    )
    r = out["result"]
    assert r["open"] == 1 and r["arrow"] == 1
    assert r["span"] >= 2  # the solid bar of the observed run, in each chart
    assert r["rows"] == 4  # three broken runs and one repaired break
    assert any("censored, never seen (n=25)" in t for t in r["tip"])


def test_chart_has_one_row_per_broken_run(page: str) -> None:
    ctx = build_context(make_summary())
    assert ctx["lag_chart"] is not None
    assert [b.kind for b in ctx["lag_chart"].bars] == ["observed", "censored", "instant"]
    chart = data_of(page)["lag_chart"]
    assert chart["axis_max"] == 30 and chart["ticks"][0] == 0
    assert [b["end"] for b in chart["bars"]] == [30, 25, 20]


def test_repaired_breaks_are_a_separate_labelled_panel(page: str) -> None:
    assert "Repaired breaks (a separate series)" in page
    assert "Never pool this series" in page
    assert "oracle error at step 0" in page  # the unfound episode is listed, not drawn
    ctx = build_context(make_summary())
    assert len(ctx["episode_chart"].bars) == 1  # only the bisected episode is drawn
    assert len(data_of(page)["episodes"]) == 2


@needs_node
def test_app_draws_every_section(page_file: Path) -> None:
    text = run_page(page_file)["text"]
    for heading in (
        "AgentBisect",
        "Headline numbers",
        "Detection lag in broken runs",
        "Repaired breaks (a separate series)",
        "Excluded runs",
        "What this can and cannot claim",
    ):
        assert heading in text
    assert "git bisect for coding agents" in text
    assert "75%" in text and "95% CI [30%, 95%]" in text and "not estimable" in text
    assert "simulation, not real agent runs" in text
    assert "t04-flash-0" in text and "Alan resumed an earlier conversation" in text
    assert "did not (3 bisectable, 2 observed)" in text
    assert "Nothing about model size" in text


@needs_node
def test_runs_table_sorts_and_filters(page_file: Path) -> None:
    out = run_page(
        page_file,
        """
        const table = () => H.byTag("table").filter((t) => t.textContent.includes("Sabotage correct"))[0];
        const ids = () => table().children[1].children.map((tr) => tr.children[1].textContent);
        const button = (label) => H.byTag("button").filter((b) => b.textContent.startsWith(label))[0];
        const seen = { start: ids() };
        H.click(button("Steps"));
        await H.tick(30);
        seen.asc = ids();
        H.click(button("Steps"));
        await H.tick(30);
        seen.desc = ids();
        H.click(button("Lag"));
        await H.tick(30);
        seen.lag = ids();
        const filter = H.byTag("input").filter((i) => i.getAttribute("aria-label") === "Filter runs")[0];
        H.input(filter, "s01");
        await H.tick(30);
        seen.filtered = ids();
        seen.sort = table().children[0].children[0].children
          .map((th) => th.getAttribute("aria-sort")).filter((v) => v !== "none");
        return seen;
        """,
    )
    r = out["result"]
    assert r["start"] == ["t01-flash-1", "t04-flash-1", "t08-flash-1", "s01-flash-1"]
    assert r["asc"] == [
        "s01-flash-1",
        "t01-flash-1",
        "t08-flash-1",
        "t04-flash-1",
    ]  # 20, 21, 25, 30
    assert r["desc"] == ["t04-flash-1", "t08-flash-1", "t01-flash-1", "s01-flash-1"]
    # lag: 0 (s01), 5 (t04), then the run with no lag number sort last
    assert r["lag"][:2] == ["s01-flash-1", "t04-flash-1"]
    assert r["filtered"] == ["s01-flash-1"]
    assert r["sort"] == ["ascending"]


def test_exclusions_listed_with_reasons(page: str) -> None:
    assert "t04-flash-0" in page and "Alan resumed an earlier conversation" in page
    assert data_of(page)["excluded"] == [["t04-flash-0", "Alan resumed an earlier conversation"]]


def test_claims_box_states_the_limits(page: str) -> None:
    assert "What this can and cannot claim" in page
    assert "this fixture library" in page
    assert "flash in Alan Code" in page
    assert "Nothing about model size" in page
    assert "did not (3 bisectable, 2 observed)" in page  # threshold not met


@needs_node
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
    path = render_study(s, tmp_path)
    text = path.read_text()
    assert EVIL not in text
    assert "&lt;script&gt;" in text  # the no-script text is escaped
    blob = DATA_RE.search(text)
    assert blob is not None and "<" not in blob.group(1)
    out = run_page(path)
    assert "script" not in out["tags"] and "img" not in out["tags"]
    assert EVIL in out["text"]  # drawn as text


def test_empty_summary_renders(tmp_path: Path) -> None:
    text = render_study(StudySummary(), tmp_path).read_text()
    assert "No runs." in text and "No metrics were computed." in text
    assert "nothing to draw" in text
    assert data_of(text)["lag_chart"] is None


@needs_node
def test_empty_summary_draws(tmp_path: Path) -> None:
    out = run_page(render_study(StudySummary(), tmp_path))
    assert "No runs." in out["text"] and "No metrics were computed." in out["text"]
    assert "nothing to draw" in out["text"]


def test_only_green_runs_renders_without_chart(tmp_path: Path) -> None:
    s = make_summary()
    s.rows = [s.rows[0]]
    s.episodes = []
    text = render_study(s, tmp_path).read_text()
    assert "nothing to draw" in text
    assert data_of(text)["lag_chart"] is None and data_of(text)["episode_chart"] is None


# --------------------------------------------------------------------------- landing cards


def _make_demo_runs(root: Path) -> None:
    """Three hand-named runs (a culprit, a repaired break, a green run) and one stamped run."""
    check = "python -m pytest -q tests/test_calc.py::test_add"
    broken = make_synthetic_run(root / "demo-broken", n_steps=10, break_at=4, observe_at=7)
    make_synthetic_run(root / "demo-fixed", n_steps=10, break_at=4, fix_at=9, observe_at=7)
    make_synthetic_run(root / "demo-green", n_steps=6, break_at=6)
    make_synthetic_run(root / "t01-flash-20261026-095102", n_steps=6, break_at=6)
    make_synthetic_run(root / "x-scratch", n_steps=6, break_at=6)
    culprit = CulpritReport(
        run_id="demo-broken",
        check=check,
        bisect=BisectResult(check=check, status=BisectStatus.FOUND, decisive_step=4),
        analysis=Analysis(
            t_err=4, t_obs=7, lag=3, censored=False, steps_after_err=6, wasted_tokens=900,
            total_tokens=3000,
        ),
    )  # fmt: skip
    (root / "demo-broken" / "bisect.json").write_text(
        RunReport(run_id="demo-broken", checks=[check], culprits=[culprit]).model_dump_json()
    )
    episode = RepairEpisode(run_id="demo-fixed", check=check, seen_at=7, t_err=4, lag=3)
    (root / "demo-fixed" / "bisect.json").write_text(
        RunReport(run_id="demo-fixed", checks=[check], repaired=[episode]).model_dump_json()
    )
    (root / "demo-green" / "bisect.json").write_text(
        RunReport(run_id="demo-green", checks=[], auto_oracle=True).model_dump_json()
    )
    assert broken.n == 10
    for run in (
        "demo-broken",
        "demo-fixed",
        "demo-green",
        "t01-flash-20261026-095102",
        "x-scratch",
    ):
        render_report(root / run)


def test_landing_cards_show_the_hand_named_runs(tmp_path: Path) -> None:
    _make_demo_runs(tmp_path)
    s = make_summary()
    s.excluded["demo-green"] = "own-repo run without a planted break: a case study"
    linkable = {"demo-broken", "demo-fixed", "t01-flash-20261026-095102", "x-scratch"}
    out = render_study(s, tmp_path / "study", link_prefix="../", linkable=linkable)
    cards = data_of(out.read_text())["featured"]
    # the culprit first, then the break the agent fixed, then the run that ended green
    assert [c["run_id"] for c in cards] == ["demo-broken", "demo-fixed", "demo-green"]
    assert [c["tag"] for c in cards] == [
        "Planted break found",  # the synthetic run plants its break at step 4
        "Caught and fixed",
        "Ended green",
    ]
    assert cards[0]["sentences"][0] == "Step 4 (edit calc/__init__.py) broke test_add."
    assert cards[0]["href"] == "../demo-broken/report.html"
    assert "stamped" not in json.dumps(cards) and "t01-flash" not in json.dumps(cards)
    assert "x-scratch" not in json.dumps(cards)  # scratch runs are never featured


@needs_node
def test_landing_cards_are_drawn_and_link_to_the_reports(tmp_path: Path) -> None:
    _make_demo_runs(tmp_path)
    out = render_study(
        make_summary(),
        tmp_path / "study",
        link_prefix="../",
        linkable={"demo-broken", "demo-fixed", "demo-green"},
    )
    r = run_page(
        out,
        """
        return H.byClass("runcard").map((a) => [a.getAttribute("href"), a.textContent]);
        """,
    )
    cards = r["result"]
    assert [c[0] for c in cards] == [
        f"../{i}/report.html" for i in ("demo-broken", "demo-fixed", "demo-green")
    ]
    assert "Step 4 (edit calc/__init__.py) broke test_add." in cards[0][1]


def test_no_cards_without_a_linkable_set(tmp_path: Path) -> None:
    _make_demo_runs(tmp_path)
    out = render_study(make_summary(), tmp_path / "study")  # linkable=None: no run is vouched for
    assert data_of(out.read_text())["featured"] == []


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
