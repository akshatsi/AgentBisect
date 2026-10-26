"""Render ``runs/<id>/report.html`` from meta.json, trace.jsonl and bisect.json.

The output is one static file: inline CSS and JS, no network. Everything that came from the agent
(args, output, reasoning, diffs) is untrusted, so the template runs with autoescape on and nothing
is marked safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from agentbisect.models import (
    MUTATING_KINDS,
    BisectStatus,
    CulpritReport,
    OracleResult,
    RepairEpisode,
    RunReport,
    Step,
    ToolKind,
    Trace,
    TraceSource,
    Verdict,
)
from agentbisect.report.text import (
    check_label,
    fmt_percent,
    fmt_tokens,
    repaired_sentences,
    step_label,
)

TEMPLATE_DIR = Path(__file__).parent
TEMPLATE_NAME = "template.html.j2"

#: Longest text shown for one step's args or output in the full trace.
MAX_FIELD_CHARS = 4000
MAX_DIFF_LINES = 600

KIND_BADGE = {
    ToolKind.READ: "rd",
    ToolKind.WRITE: "wr",
    ToolKind.EDIT: "ed",
    ToolKind.SHELL: "sh",
    ToolKind.FINISH: "fin",
    ToolKind.OTHER: "oth",
}


SOURCE_LABELS = {
    TraceSource.ALAN_CODE: "Alan Code via OpenCode",
    TraceSource.CLAUDE_CODE: "Claude Code",
    TraceSource.ALAN: "own harness",
    TraceSource.SYNTHETIC: "synthetic",
}


def model_label(alias: str | None) -> str | None:
    """``alias`` plus its display name and API id from tasks/models.yaml, when known."""
    if not alias:
        return None
    try:
        from agentbisect.llm import find_model, load_models

        entry = find_model(load_models(), alias)
    except Exception:  # no models.yaml or a bad one: show the alias alone
        return alias
    if entry is None:
        return alias
    extra = [x for x in (entry.display_name, entry.api_id) if x]
    return f"{alias} ({', '.join(extra)})" if extra else alias


# --------------------------------------------------------------------------- view models


@dataclass
class Cell:
    """One step as drawn in a culprit's timeline."""

    index: int
    kind: str
    badge: str
    label: str
    mutating: bool
    tokens: int
    is_decisive: bool = False
    is_observed: bool = False
    is_broken: bool = False  # between t_err and t_obs: the agent is working on broken code
    is_sabotage: bool = False
    probe: str | None = None  # "#3 fail"
    note: str = ""
    classes: str = ""
    title: str = ""


@dataclass
class ProbeRow:
    number: int
    step: int
    role: str
    verdict: str
    runs: int
    fails: int
    seconds: float


@dataclass
class DiffLine:
    cls: str
    text: str


@dataclass
class CulpritView:
    anchor: str
    check: str
    label: str
    found: bool
    reason: str | None
    sentences: list[str] = field(default_factory=list)
    decisive_step: int | None = None
    step_label: str = ""
    analysis: dict[str, Any] | None = None
    cells: list[Cell] = field(default_factory=list)
    diff: list[DiffLine] = field(default_factory=list)
    diff_truncated: bool = False
    reasoning: str | None = None
    oracle_tail: str | None = None
    oracle_tail_step: int | None = None
    probes: list[ProbeRow] = field(default_factory=list)
    confidence: str | None = None
    non_monotone: bool = False
    transitions: list[int] = field(default_factory=list)
    policy_rule: str | None = None
    flake_rate: str | None = None
    ground_truth: str | None = None
    ground_truth_ok: bool | None = None
    explanation: str | None = None


@dataclass
class EpisodeView:
    """One break the agent saw and fixed before the end of the run."""

    anchor: str
    check: str
    label: str
    sentences: list[str]
    t_err: int | None
    seen_at: int
    reason: str | None
    lag: int | None
    tokens: str | None
    tokens_exact: str | None
    step_label: str = ""
    cells: list[Cell] = field(default_factory=list)
    diff: list[DiffLine] = field(default_factory=list)
    diff_truncated: bool = False
    reasoning: str | None = None
    seen_output: str | None = None


@dataclass
class StepView:
    index: int
    badge: str
    kind: str
    label: str
    mutating: bool
    tokens: int
    exit_code: int | None
    reasoning: str | None
    args: str
    output: str
    truncated: bool
    snapshot: str | None


# --------------------------------------------------------------------------- helpers


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... clipped, {len(text) - limit} more characters", True


def classify_diff(text: str) -> list[DiffLine]:
    """Tag each line of a unified diff for colouring."""
    lines: list[DiffLine] = []
    for raw in text.splitlines():
        if raw.startswith(("diff --git", "index ", "new file", "deleted file", "similarity")):
            cls = "meta"
        elif raw.startswith(("+++", "---")):
            cls = "file"
        elif raw.startswith("@@"):
            cls = "hunk"
        elif raw.startswith("+"):
            cls = "add"
        elif raw.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        lines.append(DiffLine(cls, raw))
    return lines


def _step_view(step: Step) -> StepView:
    args_text = json.dumps(step.args, indent=2, ensure_ascii=False, default=str)
    args_text, t1 = _clip(args_text)
    output, t2 = _clip(step.output or "")
    return StepView(
        index=step.index,
        badge=KIND_BADGE.get(step.kind, "oth"),
        kind=step.kind.value,
        label=step_label(step),
        mutating=step.kind in MUTATING_KINDS,
        tokens=step.tokens,
        exit_code=step.exit_code,
        reasoning=step.reasoning,
        args=args_text,
        output=output,
        truncated=t1 or t2,
        snapshot=step.snapshot[:10] if step.snapshot else None,
    )


def _probe_rows(culprit: CulpritReport) -> list[ProbeRow]:
    b = culprit.bisect
    rows: list[ProbeRow] = []
    for i, probe in enumerate(b.probes, 1):
        rows.append(
            ProbeRow(
                number=i,
                step=probe.step_index,
                role="endpoint" if i <= b.precondition_probes else "search",
                verdict=probe.verdict.value,
                runs=probe.n_runs,
                fails=probe.n_fail,
                seconds=sum(r.duration_s for r in probe.runs),
            )
        )
    return rows


def _oracle_tail(culprit: CulpritReport, n_steps: int) -> tuple[str | None, int | None]:
    """Output of a failing oracle run: at the decisive step if probed, else the latest failure."""
    fails: list[OracleResult] = [p for p in culprit.bisect.probes if p.verdict is Verdict.FAIL]
    if not fails:
        return None, None
    decisive = culprit.bisect.decisive_step
    pick = next((p for p in fails if p.step_index == decisive), None)
    if pick is None:
        pick = max(fails, key=lambda p: p.step_index)
    run = next((r for r in pick.runs if r.exit_code not in (0, None)), None)
    if run is None and pick.runs:
        run = pick.runs[-1]
    if run is None or not run.output_tail.strip():
        return None, pick.step_index
    return run.output_tail, pick.step_index


def _cells(trace: Trace, culprit: CulpritReport) -> list[Cell]:
    b = culprit.bisect
    a = culprit.analysis
    t_err = b.decisive_step
    t_obs = a.t_obs if a is not None else None
    censored = a is not None and (a.censored or a.t_obs is None)
    probe_at: dict[int, str] = {}
    for i, p in enumerate(b.probes, 1):
        probe_at.setdefault(p.step_index, f"#{i} {p.verdict.value}")
    sabotage = trace.meta.sabotage_step
    cells: list[Cell] = []
    for step in trace.steps:
        i = step.index
        mutating = step.kind in MUTATING_KINDS
        is_decisive = t_err is not None and i == t_err
        is_obs = t_obs is not None and i == t_obs
        is_broken = False
        if t_err is not None and i > t_err and a is not None:
            is_broken = censored or (t_obs is not None and i < t_obs)
        notes = []
        if is_decisive:
            notes.append("decisive step")
        if is_obs:
            notes.append("agent first sees the failure")
        if is_broken:
            notes.append("working on broken code")
        if sabotage is not None and i == sabotage:
            notes.append("sabotage injected here")
        classes = [step.kind.value, "mut" if mutating else "ro"]
        if is_decisive:
            classes.append("decisive")
        if is_obs:
            classes.append("observed")
        if is_broken:
            classes.append("broken")
        if sabotage is not None and i == sabotage:
            classes.append("sabotage")
        label = step_label(step)
        cells.append(
            Cell(
                index=i,
                kind=step.kind.value,
                badge=KIND_BADGE.get(step.kind, "oth"),
                label=label,
                mutating=mutating,
                tokens=step.tokens,
                is_decisive=is_decisive,
                is_observed=is_obs,
                is_broken=is_broken,
                is_sabotage=sabotage is not None and i == sabotage,
                probe=probe_at.get(i),
                note="; ".join(notes),
                classes=" ".join(classes),
                title=f"Step {i}: {label}" + (f" ({'; '.join(notes)})" if notes else ""),
            )
        )
    return cells


def _diff_for(culprit: CulpritReport, trace: Trace, run_dir: Path) -> str:
    if culprit.diff:
        return culprit.diff
    t = culprit.bisect.decisive_step
    if t is None:
        return ""
    try:
        from agentbisect import snapshot

        return snapshot.diff(run_dir, trace.snapshot_at(t - 1), trace.snapshot_at(t))
    except Exception:  # shadow repo missing or damaged: the report still renders without a diff
        return ""


def _culprit_view(n: int, culprit: CulpritReport, trace: Trace, run_dir: Path) -> CulpritView:
    from agentbisect.report.text import verdict_sentences

    b = culprit.bisect
    found = b.status is BisectStatus.FOUND and b.decisive_step is not None
    t = b.decisive_step
    step = None
    if found and t is not None and 1 <= t <= trace.n:
        step = culprit.step or trace.steps[t - 1]
    view = CulpritView(
        anchor=f"culprit-{n}",
        check=culprit.check,
        label=check_label(culprit.check),
        found=found,
        reason=b.reason,
        decisive_step=t,
        probes=_probe_rows(culprit),
        non_monotone=b.non_monotone,
        transitions=list(b.transitions),
        explanation=culprit.explanation,
    )
    if b.policy is not None:
        view.policy_rule = b.policy.rule
        view.flake_rate = f"{b.policy.flake_rate:.2f}"
    if b.confidence is not None:
        view.confidence = f"{b.confidence * 100:.1f}%" if b.confidence < 0.999 else ">99.9%"
    tail, tail_step = _oracle_tail(culprit, trace.n)
    view.oracle_tail, view.oracle_tail_step = (tail, tail_step)
    if not found:
        return view

    view.step_label = step_label(step) if step else ""
    view.sentences = verdict_sentences(culprit, step)
    a = culprit.analysis
    if a is not None:
        view.analysis = {
            "t_err": a.t_err,
            "t_obs": a.t_obs,
            "lag": a.lag,
            "censored": a.censored or a.t_obs is None,
            "steps_after_err": a.steps_after_err,
            "wasted_tokens": fmt_tokens(a.wasted_tokens),
            "wasted_tokens_exact": f"{a.wasted_tokens:,}",
            "total_tokens": fmt_tokens(a.total_tokens),
            "share": fmt_percent(a.wasted_share),
            "share_pct": round(a.wasted_share * 100, 1),
            "signal": (a.observed_signal or "").strip().strip("_= ").strip() or a.observed_signal,
        }
    view.cells = _cells(trace, culprit)
    diff_text = _diff_for(culprit, trace, run_dir)
    diff_lines = classify_diff(diff_text)
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        view.diff_truncated = True
    view.diff = diff_lines
    if step is not None:
        view.reasoning = step.reasoning
    gt = trace.meta.sabotage_step
    if gt is not None:
        view.ground_truth = f"Sabotage was injected at step {gt}."
        view.ground_truth_ok = gt == t
    return view


def _tail_lines(text: str, limit: int = 40) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) > limit:
        lines = [f"... {len(lines) - limit} earlier lines left out", *lines[-limit:]]
    return _clip("\n".join(lines))[0]


def _episode_cells(trace: Trace, ep: RepairEpisode) -> list[Cell]:
    cells: list[Cell] = []
    for step in trace.steps:
        i = step.index
        mutating = step.kind in MUTATING_KINDS
        notes = []
        classes = [step.kind.value, "mut" if mutating else "ro"]
        if ep.t_err is not None and i == ep.t_err:
            notes.append("broke it, fixed later")
            classes.append("fixbreak")
        if i == ep.seen_at:
            notes.append("agent sees the failure")
            classes.append("fixseen")
        if ep.t_err is not None and ep.t_err < i < ep.seen_at:
            notes.append("working on broken code")
            classes.append("fixbetween")
        label = step_label(step)
        cells.append(
            Cell(
                index=i,
                kind=step.kind.value,
                badge=KIND_BADGE.get(step.kind, "oth"),
                label=label,
                mutating=mutating,
                tokens=step.tokens,
                note="; ".join(notes),
                classes=" ".join(classes),
                title=f"Step {i}: {label}" + (f" ({'; '.join(notes)})" if notes else ""),
            )
        )
    return cells


def _episode_view(n: int, ep: RepairEpisode, trace: Trace, run_dir: Path) -> EpisodeView:
    t = ep.t_err
    step = trace.steps[t - 1] if t is not None and 1 <= t <= trace.n else None
    view = EpisodeView(
        anchor=f"repaired-{n}",
        check=ep.check,
        label=check_label(ep.check),
        sentences=repaired_sentences(ep, step),
        t_err=t,
        seen_at=ep.seen_at,
        reason=ep.reason,
        lag=ep.lag,
        tokens=fmt_tokens(ep.wasted_tokens) if ep.wasted_tokens is not None else None,
        tokens_exact=f"{ep.wasted_tokens:,}" if ep.wasted_tokens is not None else None,
    )
    view.cells = _episode_cells(trace, ep)
    if 1 <= ep.seen_at <= trace.n:
        out = trace.steps[ep.seen_at - 1].output or ""
        view.seen_output = _tail_lines(out) if out.strip() else None
    if step is None or t is None:
        return view
    view.step_label = step_label(step)
    view.reasoning = step.reasoning
    try:
        from agentbisect import snapshot

        text = snapshot.diff(run_dir, trace.snapshot_at(t - 1), trace.snapshot_at(t))
    except Exception:  # shadow repo missing or damaged: the section still renders without a diff
        text = ""
    lines = classify_diff(text)
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES]
        view.diff_truncated = True
    view.diff = lines
    return view


# --------------------------------------------------------------------------- entry point


def build_context(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir)
    trace = Trace.load(run_dir)
    bisect_path = run_dir / "bisect.json"
    report: RunReport | None = None
    if bisect_path.exists():
        report = RunReport.model_validate_json(bisect_path.read_text())

    culprits = report.culprits if report else []
    views = [_culprit_view(i + 1, c, trace, run_dir) for i, c in enumerate(culprits)]
    found = [v for v in views if v.found]
    episodes = [
        _episode_view(i + 1, e, trace, run_dir)
        for i, e in enumerate(report.repaired if report else [])
    ]
    m = trace.meta
    return {
        "meta": m,
        "source_label": SOURCE_LABELS.get(m.source, m.source.value),
        "model_label": model_label(m.model),
        "n_steps": trace.n,
        "total_tokens": fmt_tokens(trace.total_tokens),
        "n_mutating": sum(1 for s in trace.steps if s.kind in MUTATING_KINDS),
        "cost": f"${m.cost_usd:.4f}" if m.cost_usd else None,
        "created": m.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        "has_bisect": report is not None,
        "auto_oracle": bool(report and report.auto_oracle),
        "culprits": views,
        "n_found": len(found),
        "many": len(views) > 1,
        "episodes": episodes,
        "steps": [_step_view(s) for s in trace.steps],
    }


def render_report(run_dir: Path) -> Path:
    """Write ``run_dir/report.html`` and return its path."""
    run_dir = Path(run_dir)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)
    html = template.render(**build_context(run_dir))
    out = run_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out
