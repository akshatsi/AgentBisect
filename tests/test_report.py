"""The HTML report: built from a synthetic run, no LLM, no network."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from agentbisect.bisect import bisect_run
from agentbisect.models import (
    Analysis,
    BisectResult,
    BisectStatus,
    CulpritReport,
    OracleRun,
    RunReport,
    Trace,
)
from agentbisect.report.render import classify_diff, render_report
from agentbisect.report.text import check_label, fmt_percent, fmt_tokens, verdict_sentences
from agentbisect.synthetic import make_synthetic_run, pytest_failure_output

CHECK = "python -m pytest -q tests/test_calc.py::test_add"
BREAK_AT, OBSERVE_AT, N = 4, 7, 10


@pytest.fixture(scope="module")
def bisected(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 10-step synthetic run, bisected with a stand-in oracle (no pytest subprocesses). Tests copy it before editing."""
    run = tmp_path_factory.mktemp("rep") / "run"
    trace = make_synthetic_run(run, n_steps=N, break_at=BREAK_AT, observe_at=OBSERVE_AT)
    good = {trace.meta.base_snapshot} | {s.snapshot for s in trace.steps[: BREAK_AT - 1]}
    tail = pytest_failure_output(-1)

    def factory(_check: str):
        def execute(sha: str) -> OracleRun:  # a stand-in for running pytest on the snapshot
            return OracleRun(exit_code=0 if sha in good else 1, duration_s=0.01, output_tail=tail)

        return execute

    report = bisect_run(trace, run, CHECK, executor_factory=factory)
    (run / "bisect.json").write_text(report.model_dump_json(indent=2))
    return run


@pytest.fixture
def run_copy(bisected: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "run"
    shutil.copytree(bisected, dest, ignore=shutil.ignore_patterns("scratch", "workdir"))
    return dest


def html_of(run: Path) -> str:
    return render_report(run).read_text(encoding="utf-8")


def load_report(run: Path) -> RunReport:
    return RunReport.model_validate_json((run / "bisect.json").read_text())


def save_report(run: Path, report: RunReport) -> None:
    (run / "bisect.json").write_text(report.model_dump_json(indent=2))


# --------------------------------------------------------------------------- the page


def test_header_shows_source_and_model(run_copy: Path) -> None:
    meta = (run_copy / "meta.json").read_text()
    meta = meta.replace('"source": "synthetic"', '"source": "alan_code"')
    meta = meta.replace('"model": null', '"model": "flash"')
    (run_copy / "meta.json").write_text(meta)
    page = html_of(run_copy)
    assert "recorded by Alan Code via OpenCode" in page
    assert "<code>flash" in page


def test_source_labels_cover_every_source() -> None:
    from agentbisect.models import TraceSource
    from agentbisect.report.render import SOURCE_LABELS

    assert set(SOURCE_LABELS) == set(TraceSource)
    assert SOURCE_LABELS[TraceSource.CLAUDE_CODE] == "Claude Code"
    assert SOURCE_LABELS[TraceSource.ALAN] == "own harness"


def test_render_writes_one_html_file(run_copy: Path) -> None:
    before = {p.name for p in run_copy.iterdir()}
    out = render_report(run_copy)
    assert out == run_copy / "report.html"
    assert out.read_text().lstrip().lower().startswith("<!doctype html>")
    assert {p.name for p in run_copy.iterdir()} - before == {"report.html"}


def test_banner_names_step_test_lag_and_tokens(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert f"Step {BREAK_AT} (edit calc/__init__.py) broke test_add." in page
    assert f"The agent only noticed at step {OBSERVE_AT}" in page
    assert f"{OBSERVE_AT - BREAK_AT} steps" in page
    assert "Wasted tokens" in page and "of run" in page


def test_page_works_offline(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", page, re.I)
    assert "<link" not in page.lower()
    assert not re.search(r"<script[^>]*\ssrc=", page, re.I)
    assert "@import" not in page and "url(" not in page


def test_page_has_dark_mode_and_phone_layout(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert "prefers-color-scheme: dark" in page
    assert 'name="viewport"' in page
    assert "max-width: 520px" in page


def test_timeline_marks_decisive_observed_and_broken_steps(run_copy: Path) -> None:
    page = html_of(run_copy)
    strip = re.findall(r'<a role="listitem" class="([^"]*)" href="#culprit-1-s(\d+)"', page)
    classes = {int(i): c.split() for c, i in strip}
    assert len(classes) == N
    assert "decisive" in classes[BREAK_AT]
    assert "observed" in classes[OBSERVE_AT]
    between = range(BREAK_AT + 1, OBSERVE_AT)
    assert all("broken" in classes[i] for i in between)
    assert not any("broken" in classes[i] for i in (1, BREAK_AT, OBSERVE_AT))
    assert "working on broken code" in page


def test_diff_lines_are_coloured(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert '<span class="del">-    return a + b</span>' in page
    assert '<span class="add">+    return a - b</span>' in page
    assert 'class="hunk"' in page


def test_reasoning_probe_log_and_confidence_shown(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert "Simplify add()." in page  # the agent's text before the decisive step
    assert "Probe log" in page
    assert re.search(r'<span class="pill fail">fail</span>', page)
    assert re.search(r'<span class="pill pass">pass</span>', page)
    assert "Confidence" in page
    assert "No sign of a non-monotone history" in page


def test_oracle_output_tail_shown(run_copy: Path) -> None:
    assert "Oracle output at step" in html_of(run_copy)


def test_sabotage_ground_truth_shown(run_copy: Path) -> None:
    page = html_of(run_copy)
    assert f"Sabotage was injected at step {BREAK_AT}." in page
    assert "Bisect found the same step." in page
    assert "tag sab" in page


def test_ground_truth_mismatch_is_flagged(run_copy: Path) -> None:
    meta = (run_copy / "meta.json").read_text()
    (run_copy / "meta.json").write_text(
        meta.replace(f'"sabotage_step": {BREAK_AT}', f'"sabotage_step": {BREAK_AT + 1}')
    )
    assert "Bisect found a different step." in html_of(run_copy)


def test_no_ai_box_without_explanation(run_copy: Path) -> None:
    assert "AI explanation" not in html_of(run_copy)


def test_ai_explanation_is_labelled(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].explanation = "The edit flipped the sign."
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert "AI explanation" in page
    assert "The edit flipped the sign." in page
    assert "does not affect the verdict" in page


def test_censored_lag_says_never_noticed(run_copy: Path) -> None:
    rep = load_report(run_copy)
    a = rep.culprits[0].analysis
    assert a is not None
    rep.culprits[0].analysis = a.model_copy(
        update={"t_obs": None, "lag": None, "censored": True, "observed_signal": None}
    )
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert "The agent never noticed." in page
    assert "censored" in page
    classes = re.findall(r'<a role="listitem" class="([^"]*)" href="#culprit-1-s(\d+)"', page)
    shaded = [int(i) for c, i in classes if "broken" in c.split()]
    assert shaded == list(range(BREAK_AT + 1, N + 1))


def test_not_bisectable_explains_why(run_copy: Path) -> None:
    (run_copy / "bisect.json").write_text(
        RunReport(
            run_id="x",
            checks=["true"],
            culprits=[
                CulpritReport(
                    run_id="x",
                    check="true",
                    bisect=BisectResult(
                        check="true",
                        status=BisectStatus.NOT_BISECTABLE,
                        reason="oracle does not fail at the final step 10",
                    ),
                )
            ],
        ).model_dump_json()
    )
    page = html_of(run_copy)
    assert "Not bisectable" in page
    assert "oracle does not fail at the final step 10" in page
    assert "Timeline" not in page


def test_multiple_culprits_get_one_section_each(run_copy: Path) -> None:
    rep = load_report(run_copy)
    second = rep.culprits[0].model_copy(deep=True)
    second.check = "python -m pytest -q tests/test_calc.py::test_other"
    rep.culprits.append(second)
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert page.count('<section id="culprit-') == 2
    assert 'id="culprit-1"' in page and 'id="culprit-2"' in page
    assert "test_other" in page
    assert "2 of 2 checks bisected" in page


def test_non_monotone_note(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].bisect.non_monotone = True
    rep.culprits[0].bisect.transitions = [2, BREAK_AT]
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert "Non-monotone history" in page
    assert "steps 2, 4" in page


def test_missing_bisect_json_still_renders(run_copy: Path) -> None:
    (run_copy / "bisect.json").unlink()
    page = html_of(run_copy)
    assert "Not bisected yet" in page
    assert "Full trace" in page


def test_empty_culprit_list_renders(run_copy: Path) -> None:
    save_report(run_copy, RunReport(run_id="x", checks=[], culprits=[], auto_oracle=True))
    assert "No culprit found" in html_of(run_copy)


def test_missing_shadow_repo_still_renders_without_diff(run_copy: Path) -> None:
    rep = load_report(run_copy)
    rep.culprits[0].diff = ""
    save_report(run_copy, rep)
    shutil.rmtree(run_copy / "shadow.git")
    assert "No file changes in this step" in html_of(run_copy)


# --------------------------------------------------------------------------- untrusted text


def test_trace_content_is_escaped(run_copy: Path) -> None:
    evil = "<script>alert('x')</script>"
    trace = Trace.load(run_copy)
    trace.steps[0].output = f"{evil} </pre><img src=x onerror=alert(1)>"
    trace.steps[0].reasoning = evil
    trace.steps[0].args = {"path": evil}
    trace.steps[BREAK_AT - 1].reasoning = f"{evil} </pre>"
    trace.save(run_copy)
    rep = load_report(run_copy)
    rep.culprits[0].diff = f"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-{evil}\n+</pre><b>x</b>\n"
    rep.culprits[0].explanation = evil
    rep.culprits[0].check = f"pytest {evil}"
    rep.culprits[0].step = None
    save_report(run_copy, rep)
    page = html_of(run_copy)
    assert evil not in page
    assert "<img src=x" not in page
    assert "<b>x</b>" not in page
    assert page.lower().count("<script") == 1  # only our own theme script
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in page


# --------------------------------------------------------------------------- helpers


def test_classify_diff_tags_lines() -> None:
    lines = classify_diff(
        "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"
    )
    assert [line.cls for line in lines] == [
        "meta", "meta", "file", "file", "hunk", "ctx", "del", "add",
    ]  # fmt: skip


def test_fmt_helpers() -> None:
    assert fmt_tokens(830) == "830"
    assert fmt_tokens(41_200) == "41k"
    assert fmt_tokens(1_500) == "1.5k"
    assert fmt_tokens(2_000) == "2k"
    assert fmt_tokens(1_250_000) == "1.2M"
    assert fmt_percent(0.5) == "50%"
    assert fmt_percent(0.034) == "3.4%"


def test_check_label_picks_the_test_name() -> None:
    assert check_label("pytest tests/test_auth.py::test_login -q") == "test_login"
    assert check_label("python -m pytest tests/test_auth.py") == "tests/test_auth.py"
    assert check_label("make check") == "make check"
    assert check_label("pytest 'unclosed") == "pytest 'unclosed"


def test_verdict_sentences_cover_lag_zero_and_censored() -> None:
    b = BisectResult(check="pytest a.py::test_x", status=BisectStatus.FOUND, decisive_step=9)
    base = {"t_err": 9, "steps_after_err": 3, "wasted_tokens": 41_000, "total_tokens": 100_000}
    now = Analysis(t_obs=9, lag=0, censored=False, **base)
    never = Analysis(t_obs=None, lag=None, censored=True, **base)
    c = CulpritReport(run_id="r", check=b.check, bisect=b, analysis=now)
    assert "saw the failure at once" in " ".join(verdict_sentences(c, None))
    c.analysis = never
    text = " ".join(verdict_sentences(c, None))
    assert "The agent never noticed." in text
    assert "41k tokens" in text and "41%" in text
    c.analysis = None
    assert verdict_sentences(c, None) == ["Step 9 broke test_x."]
