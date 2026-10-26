"""Render the study page (``study.html``) and the draft results text (``RESULTS.draft.md``).

Input is a ``StudySummary``. Both outputs are static: the page is one file with inline CSS and
JS (Preact, see ``page.py``) and no network use. Run ids, task ids, checks and exclusion reasons
come from disk, so they travel as escaped JSON text and the app draws them as text nodes. Every
number shown comes from ``summary.rows``, ``summary.episodes`` or ``summary.metrics``; this module
computes counts and ratios of those rows, never a statistic.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentbisect.models import Metric, RepairEpisode, StudyRow, StudySummary
from agentbisect.report.page import render_page
from agentbisect.report.stable import write_text_stable
from agentbisect.report.text import check_label

TEMPLATE_NAME = "study.html.j2"
STYLES = ("study.css",)
APP = "study_app.js"
#: The landing page shows at most this many run cards.
MAX_FEATURED = 3

#: Pooled lag statistics need at least this many bisectable broken runs, and this many observed.
POOLED_MIN_BROKEN = 6
POOLED_MIN_OBSERVED = 3

#: A metric whose name has one of these words and whose value lies in [0, 1] is shown as a percent.
_PERCENT_WORDS = ("share", "rate", "accuracy", "proportion", "fraction")

# --------------------------------------------------------------------------- number formatting


def _num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    if abs(x) >= 100:
        return f"{x:.0f}"
    if abs(x) >= 10:
        return f"{x:.1f}"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _is_percent(metric: Metric) -> bool:
    name = metric.name.lower()
    if not any(w in name for w in _PERCENT_WORDS):
        return False
    values = [v for v in (metric.value, metric.ci_low, metric.ci_high) if v is not None]
    return bool(values) and all(0.0 <= v <= 1.0 for v in values)


def _fmt(metric: Metric, v: float) -> str:
    if _is_percent(metric):
        pct = v * 100
        return f"{pct:.0f}%" if pct >= 10 or pct == 0 else f"{pct:.1f}%"
    return _num(v)


def metric_value(metric: Metric) -> str:
    """The point estimate as text, or ``not estimable``."""
    return "not estimable" if metric.value is None else _fmt(metric, metric.value)


def metric_interval(metric: Metric) -> str:
    """``95% CI [lo, hi]`` or an empty string when there is no interval."""
    if metric.ci_low is None or metric.ci_high is None:
        return ""
    return f"95% CI [{_fmt(metric, metric.ci_low)}, {_fmt(metric, metric.ci_high)}]"


def is_simulation(metric: Metric) -> bool:
    text = f"{metric.name} {metric.method} {metric.note or ''}".lower()
    return "simulat" in text


def _share(row: StudyRow) -> float | None:
    if row.wasted_tokens is None or not row.total_tokens:
        return None
    return row.wasted_tokens / row.total_tokens


def share_text(row: StudyRow) -> str:
    s = _share(row)
    if s is None:
        return "-"
    pct = s * 100
    return f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%"


def lag_text(row: StudyRow) -> str:
    if row.t_err is None:
        return "-"
    if row.censored or row.t_obs is None:
        return f"censored (never seen, {row.steps_after_err if row.steps_after_err is not None else '?'} steps)"
    return str(row.lag if row.lag is not None else row.t_obs - row.t_err)


def broken_text(row: StudyRow) -> str:
    if not row.final_failed:
        return "no"
    return "yes" if row.bisectable else "yes, not bisectable"


def _group_by_run(rows: list[StudyRow]) -> dict[str, list[StudyRow]]:
    out: dict[str, list[StudyRow]] = {}
    for r in rows:
        out.setdefault(r.run_id, []).append(r)
    return out


def sabotage_outcomes(
    rows: list[StudyRow], episodes: list[RepairEpisode]
) -> dict[str, bool | None]:
    """Per sabotage run: did the bisect name the injected step? ``None`` = not rated.

    A run is rated when it fails at the end or shows a break the agent saw. It is right when its
    final-state culprit, or an episode (a break seen, then fixed), has the injected step as its
    decisive step. Not bisectable counts as wrong. Used by the accuracy metric (``study.summarize``) and by every table that shows the verdict.
    """
    runs = _group_by_run([r for r in rows if r.sabotage_step is not None])
    out: dict[str, bool | None] = {}
    for rid, rs in runs.items():
        injected = rs[0].sabotage_step
        mine = [e for e in episodes if e.run_id == rid]
        if not (any(r.final_failed for r in rs) or mine):
            out[rid] = None
            continue
        by_final = any(r.bisectable and r.sabotage_correct for r in rs)
        out[rid] = bool(by_final or any(e.t_err == injected for e in mine))
    return out


def sabotage_text(row: StudyRow, outcomes: dict[str, bool | None]) -> str:
    """The sabotage verdict of a row's run, by the rule the accuracy metric uses.

    ``outcomes`` comes from ``agentbisect.study.sabotage_outcomes``: the run is right when its
    final culprit or a repaired episode has the planted step as its decisive step.
    """
    if row.sabotage_step is None:
        return "-"
    outcome = outcomes.get(row.run_id)
    if outcome is None:
        return "no break seen"
    return f"found step {row.sabotage_step}" if outcome else f"missed step {row.sabotage_step}"


def sabotage_class(text: str) -> str:
    return {"found": "pass", "missed": "fail"}.get(text.split(" ", 1)[0], "")


# --------------------------------------------------------------------------- terminal table

LABEL_LIMIT = 20  # characters of a test name shown in the terminal table


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "\u2026"


_STAMP = re.compile(r"-\d{8}-(\d{4})\d{2}$")


def run_labels(rows: list[StudyRow]) -> dict[str, str]:
    """Short, unique label per run id: the task id (or repo name) plus the start time as HHMM.

    ``t01-flash-20261026-095102`` becomes ``t01-0951``. A run id without a time stamp keeps the
    bare task id; two runs that still collide get ``-2``, ``-3``.
    """
    labels: dict[str, str] = {}
    seen: dict[str, int] = {}
    for r in rows:
        if r.run_id in labels:
            continue
        m = _STAMP.search(r.run_id)
        base = f"{r.task_id}-{m.group(1)}" if m else r.task_id
        seen[base] = seen.get(base, 0) + 1
        labels[r.run_id] = base if seen[base] == 1 else f"{base}-{seen[base]}"
    return labels


@dataclass
class TableRow:
    """One line of the terminal run table: rows of one run that share a decisive step collapse."""

    run: str
    steps: int
    broken: str
    t_err: str
    lag: str
    wasted: str
    sabotage: str


def _short_lag(row: StudyRow) -> str:
    if row.t_err is None:
        return "-"
    if row.censored or row.t_obs is None:
        return "censored"
    return str(row.lag if row.lag is not None else row.t_obs - row.t_err)


def table_rows(summary: StudySummary, limit: int = LABEL_LIMIT) -> list[TableRow]:
    """Collapse the rows that share a run and a decisive step into one line ("3 tests")."""
    labels = run_labels(summary.rows)
    outcomes = sabotage_outcomes(summary.rows, summary.episodes)
    groups: dict[tuple[str, bool, bool, int | None], list[StudyRow]] = {}
    for r in summary.rows:
        groups.setdefault((r.run_id, r.final_failed, r.bisectable, r.t_err), []).append(r)
    out = []
    for (run_id, failed, bisectable, t_err), rs in groups.items():
        first = rs[0]
        if not failed:
            broken = "no"
        elif len(rs) > 1:
            broken = f"{len(rs)} tests"
        else:
            broken = clip(check_label(first.check) if first.check else "yes", limit)
        lags = sorted({_short_lag(r) for r in rs}, key=lambda x: (not x.isdigit(), x))
        out.append(
            TableRow(
                run=labels[run_id],
                steps=first.n_steps,
                broken=broken,
                t_err=str(t_err) if t_err is not None else ("none" if failed else "-"),
                lag="/".join(lags),
                wasted=share_text(first),
                sabotage=sabotage_text(first, outcomes),
            )
        )
        if failed and not bisectable:
            out[-1].lag = "-"
    return out


# --------------------------------------------------------------------------- counts


@dataclass
class Counts:
    runs: int
    broken_runs: int
    bisectable_broken_runs: int
    observed_broken_runs: int
    sabotage_runs: int
    sabotage_correct: int
    pooled_ok: bool
    models: list[str]
    tasks: list[str]


def count_rows(rows: list[StudyRow]) -> Counts:
    run_ids = {r.run_id for r in rows}
    broken = {r.run_id for r in rows if r.final_failed}
    bisectable = {r.run_id for r in rows if r.final_failed and r.bisectable and r.t_err is not None}
    observed = {
        r.run_id
        for r in rows
        if r.final_failed and r.bisectable and r.t_err is not None and r.censored is False
    }
    sab = {r.run_id for r in rows if r.sabotage_step is not None}
    sab_ok = {r.run_id for r in rows if r.sabotage_step is not None and r.sabotage_correct}
    return Counts(
        runs=len(run_ids),
        broken_runs=len(broken),
        bisectable_broken_runs=len(bisectable),
        observed_broken_runs=len(observed),
        sabotage_runs=len(sab),
        sabotage_correct=len(sab_ok),
        pooled_ok=len(bisectable) >= POOLED_MIN_BROKEN and len(observed) >= POOLED_MIN_OBSERVED,
        models=sorted({r.model for r in rows}),
        tasks=sorted({r.task_id for r in rows}),
    )


def _scope_sentence(c: Counts) -> str:
    models = ", ".join(c.models) if c.models else "no model"
    return (
        f"Every claim here is about {models} in Alan Code, on this fixture library and these "
        "tasks. It says nothing about other models, other agents, or other code."
    )


def claims(c: Counts) -> tuple[list[str], list[str]]:
    """The ``can claim`` and ``cannot claim`` lists, from METHODS section 8."""
    can = [
        f"How often this setup ended with a broken final state: {c.broken_runs} of {c.runs} "
        "runs, with the interval in the headline numbers.",
        "For each broken run, the exact step that broke the code and when the agent first saw it. "
        "Read each as a case study.",
        "For breaks the agent saw and later fixed (the repaired-break series), how many steps "
        "passed before it saw them.",
        "That the bisect works on real traces, from the runs where we injected a known break. "
        "A few runs show only that the pipeline works.",
    ]
    cannot = [
        _scope_sentence(c),
        "Nothing about model size or any comparison between models. One model was tested."
        if len(c.models) <= 1
        else "Differences between models are descriptive only. Too few runs per model to test them.",
        "A pooled median lag appears only with at least "
        f"{POOLED_MIN_BROKEN} bisectable broken runs, {POOLED_MIN_OBSERVED} of them observed. "
        + (
            "This study met that."
            if c.pooled_ok
            else f"This study did not ({c.bisectable_broken_runs} bisectable, "
            f"{c.observed_broken_runs} observed)."
        ),
        "The repaired-break series covers only breaks the agent saw. It says nothing about "
        "breaks the agent never saw, and we never pool it with the censored runs.",
        "Rows from the same task are not independent, so read pooled numbers with care.",
        "Only failures that a pytest test catches. A bad choice no test catches is invisible.",
    ]
    return can, cannot


# --------------------------------------------------------------------------- chart


@dataclass
class Bar:
    """One row of a lag chart. Steps are 1-based; the page scales them to the axis."""

    label: str
    title: str
    t_err: int
    t_obs: int | None  # None when censored
    end: int  # last step of the run (a censored bar reaches it)
    kind: str  # "observed" | "censored" | "instant"
    text: str
    href: str | None = None


@dataclass
class Chart:
    axis_max: int
    ticks: list[int]
    bars: list[Bar]


def _nice_step(span: int) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if span / step <= 10:
            return step
    return max(1, span // 10)


def _clip_label(text: str, limit: int = 64) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _make_chart(items: list[dict[str, Any]], axis_max: int) -> Chart:
    """``items``: label, title, t_err, t_obs (None = censored), end, text, href."""
    axis_max = max(axis_max, 1)
    bars: list[Bar] = []
    for it in items:
        t_err, t_obs = it["t_err"], it["t_obs"]
        if t_obs is None:
            kind = "censored"
        elif t_obs == t_err:
            kind = "instant"
        else:
            kind = "observed"
        bars.append(
            Bar(
                label=_clip_label(it["label"]),
                title=it["title"],
                t_err=t_err,
                t_obs=t_obs,
                end=it["end"],
                kind=kind,
                text=it["text"],
                href=it.get("href"),
            )
        )
    return Chart(
        axis_max=axis_max,
        ticks=list(range(0, axis_max + 1, _nice_step(axis_max))),
        bars=bars,
    )


def _row_chart(rows: list[StudyRow], href_for: Any) -> Chart | None:
    broken = [r for r in rows if r.final_failed and r.bisectable and r.t_err is not None]
    if not broken:
        return None
    items = []
    for r in broken:
        censored = r.censored or r.t_obs is None
        if r.no_opportunity:
            text = f"last step, no chance to see it (n={r.n_steps})"
        elif censored:
            text = f"censored, never seen (n={r.n_steps})"
        elif r.t_obs == r.t_err:
            text = "lag 0"
        else:
            text = f"lag {r.lag if r.lag is not None else (r.t_obs or 0) - (r.t_err or 0)}"
        label = f"{r.task_id}  {check_label(r.check)}" if r.check else r.task_id
        items.append(
            {
                "label": label,
                "title": f"{r.run_id}: {r.check or ''} broke at step {r.t_err}; {text}",
                "t_err": r.t_err,
                "t_obs": None if censored else r.t_obs,
                "end": r.n_steps,
                "text": text,
                "href": href_for(r.run_id),
            }
        )
    axis_max = max(max(r.n_steps for r in broken), 1)
    return _make_chart(items, axis_max)


def _episode_chart(episodes: list[RepairEpisode], href_for: Any) -> Chart | None:
    drawn = [e for e in episodes if e.t_err is not None]
    if not drawn:
        return None
    items = []
    for e in drawn:
        t_err = e.t_err or 0
        t_obs = t_err + e.lag if e.lag is not None else e.seen_at
        text = f"lag {e.lag}" if e.lag is not None else f"seen at step {e.seen_at}"
        items.append(
            {
                "label": f"{e.run_id}  {check_label(e.check)}",
                "title": f"{e.run_id}: {e.check} broke at step {t_err}, seen at step "
                f"{e.seen_at}; {text}",
                "t_err": t_err,
                "t_obs": t_obs,
                "end": e.seen_at,
                "text": text,
                "href": href_for(e.run_id),
            }
        )
    axis_max = max(max(e.seen_at for e in drawn), max(i["t_obs"] for i in items))
    return _make_chart(items, axis_max)


# --------------------------------------------------------------------------- context


@dataclass
class RunRowView:
    run_id: str
    href: str | None
    task: str
    check: str
    steps: int
    broken: str
    broken_class: str
    t_err: str
    t_obs: str
    lag: str
    censored: bool
    share: str
    sabotage: str
    sabotage_class: str
    # numbers the page sorts by; None sorts last
    t_err_n: int | None = None
    t_obs_n: int | None = None
    lag_n: float | None = None
    share_n: float | None = None


@dataclass
class MetricView:
    name: str
    value: str
    interval: str
    n: int
    method: str
    note: str | None
    simulation: bool
    estimable: bool
    # for the interval graphic: the raw numbers and whether the scale is 0..1
    raw: float | None = None
    low: float | None = None
    high: float | None = None
    percent: bool = False


@dataclass
class EpisodeView:
    run_id: str
    href: str | None
    check: str
    seen_at: int
    t_err: str
    lag: str
    wasted: str
    reason: str | None


def _href_factory(link_prefix: str, linkable: Collection[str] | None) -> Any:
    def href_for(run_id: str) -> str | None:
        if linkable is not None and run_id not in linkable:
            return None
        return f"{link_prefix}{run_id}/report.html"

    return href_for


def build_context(
    summary: StudySummary,
    *,
    link_prefix: str = "../",
    linkable: Collection[str] | None = None,
) -> dict[str, Any]:
    """Everything the page needs.

    ``link_prefix`` is the path from ``study.html`` to the folder of run folders. ``linkable``
    limits links to run ids that have a ``report.html``; ``None`` links every run.
    """
    href_for = _href_factory(link_prefix, linkable)
    counts = count_rows(summary.rows)
    can, cannot = claims(counts)

    outcomes = sabotage_outcomes(summary.rows, summary.episodes)
    run_views = []
    for r in summary.rows:
        censored = bool(r.t_err is not None and (r.censored or r.t_obs is None))
        sab = sabotage_text(r, outcomes)
        lag_n = None
        if r.t_err is not None and not censored:
            lag_n = r.lag if r.lag is not None else (r.t_obs or r.t_err) - r.t_err
        share = _share(r)
        run_views.append(
            RunRowView(
                run_id=r.run_id,
                href=href_for(r.run_id),
                task=r.task_id,
                check=check_label(r.check) if r.check else "-",
                steps=r.n_steps,
                broken=broken_text(r),
                broken_class="fail" if r.final_failed else "pass",
                t_err="-" if r.t_err is None else str(r.t_err),
                t_obs="-" if r.t_obs is None else str(r.t_obs),
                lag="last step, no chance to see it" if r.no_opportunity else lag_text(r),
                censored=censored,
                share=share_text(r),
                sabotage=sab,
                sabotage_class=sabotage_class(sab),
                t_err_n=r.t_err,
                t_obs_n=r.t_obs,
                lag_n=lag_n,
                share_n=None if share is None else round(share, 4),
            )
        )

    metric_views = [
        MetricView(
            name=m.name,
            value=metric_value(m),
            interval=metric_interval(m),
            n=m.n,
            method=m.method,
            note=m.note,
            simulation=is_simulation(m),
            estimable=m.value is not None,
            raw=m.value,
            low=m.ci_low,
            high=m.ci_high,
            percent=_is_percent(m),
        )
        for m in summary.metrics
    ]

    episode_views = [
        EpisodeView(
            run_id=e.run_id,
            href=href_for(e.run_id),
            check=check_label(e.check),
            seen_at=e.seen_at,
            t_err="-" if e.t_err is None else str(e.t_err),
            lag="-" if e.lag is None else str(e.lag),
            wasted="-" if e.wasted_tokens is None else f"{e.wasted_tokens:,}",
            reason=e.reason,
        )
        for e in summary.episodes
    ]

    return {
        "summary": summary,
        "design": summary.design,
        "created": summary.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        "counts": counts,
        "scope": _scope_sentence(counts),
        "can_claim": can,
        "cannot_claim": cannot,
        "metrics": metric_views,
        "has_simulation": any(m.simulation for m in metric_views),
        "runs": run_views,
        "excluded": sorted(summary.excluded.items()),
        "episodes": episode_views,
        "lag_chart": _row_chart(summary.rows, href_for),
        "episode_chart": _episode_chart(summary.episodes, href_for),
        "featured": [],
    }


def featured_runs(
    out_dir: Path,
    link_prefix: str,
    linkable: Collection[str] | None,
    excluded: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Cards for the hand-named runs next to the study folder (the demo runs).

    Needs ``linkable`` (the caller knows which run folders are real) and a relative
    ``link_prefix``. A run qualifies if its id is not one ``record`` made (those end in a
    ``-YYYYMMDD-HHMMSS`` stamp), it has a trace and a ``report.html`` on disk, and it is a counted
    run or an excluded case study. Runs named ``x-...`` are scratch runs and never shown.
    """
    from agentbisect.report.render import is_stamped, run_headline

    if linkable is None or "://" in link_prefix or link_prefix.startswith("/"):
        return []
    runs_dir = (Path(out_dir) / link_prefix).resolve()
    cards: list[dict[str, Any]] = []
    for run_id in sorted(set(linkable) | set(excluded)):
        if is_stamped(run_id) or run_id.startswith("x-"):
            continue
        run_dir = runs_dir / run_id
        if not all((run_dir / f).exists() for f in ("meta.json", "trace.jsonl", "report.html")):
            continue
        try:
            card = run_headline(run_dir)
        except Exception:  # a run that cannot be read is left off the landing page
            continue
        card["href"] = f"{link_prefix}{run_id}/report.html"
        cards.append(card)
    cards.sort(key=lambda c: (c["rank"], c["run_id"]))
    return cards[:MAX_FEATURED]


# --------------------------------------------------------------------------- outputs


def page_data(ctx: dict[str, Any]) -> dict[str, Any]:
    """The JSON the study page draws, from ``build_context`` output."""
    chart = ctx["lag_chart"]
    ep_chart = ctx["episode_chart"]
    return {
        "page": "study",
        "created": ctx["created"],
        "design": ctx["design"],
        "scope": ctx["scope"],
        "counts": asdict(ctx["counts"]),
        "featured": ctx["featured"],
        "metrics": [asdict(m) for m in ctx["metrics"]],
        "has_simulation": ctx["has_simulation"],
        "lag_chart": asdict(chart) if chart else None,
        "episode_chart": asdict(ep_chart) if ep_chart else None,
        "runs": [asdict(r) for r in ctx["runs"]],
        "episodes": [asdict(e) for e in ctx["episodes"]],
        "excluded": [list(x) for x in ctx["excluded"]],
        "can": ctx["can_claim"],
        "cannot": ctx["cannot_claim"],
    }


def render_study(
    summary: StudySummary,
    out_dir: Path,
    *,
    link_prefix: str = "../",
    linkable: Collection[str] | None = None,
) -> Path:
    """Write ``out_dir/study.html`` and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = build_context(summary, link_prefix=link_prefix, linkable=linkable)
    ctx["featured"] = featured_runs(out_dir, link_prefix, linkable, summary.excluded)
    html = render_page(
        TEMPLATE_NAME,
        title="AgentBisect study",
        data=page_data(ctx),
        app=APP,
        styles=STYLES,
        **ctx,
    )
    out = out_dir / "study.html"
    write_text_stable(out, html)
    return out


def _md_cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _md_table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return lines


def results_markdown(summary: StudySummary) -> str:
    """The text of ``RESULTS.draft.md``. The audited version is docs/RESULTS.md."""
    c = count_rows(summary.rows)
    can, cannot = claims(c)
    outcomes = sabotage_outcomes(summary.rows, summary.episodes)
    out: list[str] = ["# Results (draft)", ""]
    out += ["Draft generated by `agentbisect study`; the audited version is docs/RESULTS.md.", ""]
    out += [
        f"Generated {summary.created_at.strftime('%Y-%m-%d %H:%M UTC')}. "
        "Every number below comes from `study.json`.",
        "",
    ]
    if summary.design:
        out += [f"Design: {summary.design}", ""]

    out += ["## Scope", "", _scope_sentence(c), ""]
    out += [
        f"- Runs counted: {c.runs} ({len(summary.excluded)} more excluded, see below).",
        f"- Models: {', '.join(c.models) or 'none'}.",
        f"- Tasks: {', '.join(c.tasks) or 'none'}.",
        f"- Runs that ended broken: {c.broken_runs} of {c.runs}; "
        f"{c.bisectable_broken_runs} bisectable, {c.observed_broken_runs} where the agent saw "
        "the failure.",
        "",
    ]

    out += ["## Headline numbers", ""]
    if summary.metrics:
        rows = []
        for m in summary.metrics:
            label = m.name + (" (simulation, not real agent runs)" if is_simulation(m) else "")
            rows.append(
                [
                    label,
                    metric_value(m),
                    metric_interval(m) or "-",
                    m.n,
                    m.method,
                    m.note or "",
                ]
            )
        out += _md_table(["metric", "value", "interval", "n", "method", "note"], rows)
    else:
        out += ["No metrics were computed."]
    out += [""]

    out += ["## Runs", ""]
    if summary.rows:
        out += _md_table(
            [
                "task",
                "run",
                "check",
                "steps",
                "broken",
                "t_err",
                "t_obs",
                "lag",
                "wasted share",
                "sabotage",
            ],
            [
                [
                    r.task_id,
                    r.run_id,
                    check_label(r.check) if r.check else "-",
                    r.n_steps,
                    broken_text(r),
                    "-" if r.t_err is None else r.t_err,
                    "-" if r.t_obs is None else r.t_obs,
                    lag_text(r),
                    share_text(r),
                    sabotage_text(r, outcomes),
                ]
                for r in summary.rows
            ],
        )
    else:
        out += ["No runs."]
    out += [""]

    broken = [r for r in summary.rows if r.final_failed]
    out += ["## Broken runs", ""]
    if not broken:
        out += ["No run ended broken.", ""]
    for r in broken:
        what = check_label(r.check) if r.check else "the check"
        if not r.bisectable or r.t_err is None:
            out += [f"- {r.task_id} ({r.run_id}): the final state fails {what}; not bisectable."]
            continue
        if r.censored or r.t_obs is None:
            seen = f"The agent never saw it in the {r.steps_after_err} steps after (censored)."
        else:
            seen = f"The agent saw it at step {r.t_obs}, a lag of {r.lag} steps."
        waste = f" Tokens after the break: {share_text(r)} of the run." if _share(r) else ""
        out += [f"- {r.task_id} ({r.run_id}): step {r.t_err} broke {what}. {seen}{waste}"]
    out += [""]

    out += ["## Repaired breaks", ""]
    out += [
        "Breaks the agent saw and later fixed, bisected over the steps up to the sighting. "
        "None is censored, since we find them only when the agent saw them. This series is "
        "separate and is never pooled with the broken runs above.",
        "",
    ]
    if summary.episodes:
        out += _md_table(
            ["run", "check", "seen at", "t_err", "lag", "wasted tokens", "note"],
            [
                [
                    e.run_id,
                    check_label(e.check),
                    e.seen_at,
                    "-" if e.t_err is None else e.t_err,
                    "-" if e.lag is None else e.lag,
                    "-" if e.wasted_tokens is None else f"{e.wasted_tokens:,}",
                    e.reason or "",
                ]
                for e in summary.episodes
            ],
        )
        out += ["", f"Episodes: {len(summary.episodes)}."]
    else:
        out += ["No repaired breaks found."]
    out += [""]

    out += ["## Bisect accuracy on injected breaks", ""]
    if c.sabotage_runs:
        out += [
            f"The bisect found the injected step in {c.sabotage_correct} of {c.sabotage_runs} "
            "sabotage runs. A few runs show only that the pipeline works on real traces. "
            "Simulation results (not real agent runs) are in `docs/METHODS.md` section 8.5.",
            "",
        ]
    else:
        out += ["No sabotage runs in this study.", ""]

    out += ["## Excluded runs", ""]
    if summary.excluded:
        out += [f"- {rid}: {reason}" for rid, reason in sorted(summary.excluded.items())]
    else:
        out += ["None."]
    out += [""]

    out += ["## What this can and cannot claim", "", "It can claim:", ""]
    out += [f"- {s}" for s in can]
    out += ["", "It cannot claim:", ""]
    out += [f"- {s}" for s in cannot]
    out += [""]
    return "\n".join(out)


def render_results_draft(summary: StudySummary, out_dir: Path) -> Path:
    """Write ``out_dir/RESULTS.draft.md`` and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "RESULTS.draft.md"
    write_text_stable(out, results_markdown(summary))
    return out
