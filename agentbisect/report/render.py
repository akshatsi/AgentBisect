"""Render ``runs/<id>/report.html`` from meta.json, trace.jsonl and bisect.json.

The output is one static file. ``build_data`` turns a run into plain JSON (steps, per-step diffs,
verdict sentences, the replay of the bisect probes); ``assets/run_app.js`` draws it. Everything
that came from the agent (args, output, reasoning, diffs, test names) is untrusted: it travels as
escaped JSON text and the app shows it through text nodes only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentbisect.analyze import strip_ansi
from agentbisect.models import (
    MUTATING_KINDS,
    BisectResult,
    BisectStatus,
    CulpritReport,
    OracleResult,
    RepairEpisode,
    RunReport,
    Step,
    Trace,
    TraceSource,
    Verdict,
)
from agentbisect.report.page import render_page
from agentbisect.report.stable import write_text_stable
from agentbisect.report.text import (
    Shortener,
    check_label,
    fmt_percent,
    fmt_tokens,
    group_by_step,
    group_verdict_sentences,
    repaired_sentences,
    shares_analysis,
    step_label,
)

TEMPLATE_NAME = "run.html.j2"
STYLES = ("run.css",)
APP = "run_app.js"

#: Longest text kept for one step's arguments or output.
MAX_FIELD_CHARS = 4000
#: Longest agent reasoning kept for one step.
MAX_REASONING_CHARS = 8000
#: Longest diff kept for one step, and for a step the verdict is about.
MAX_STEP_DIFF = 20_000
MAX_CULPRIT_DIFF = 60_000
#: Total diff text in one page. Steps after the budget keep their text but lose the diff.
DIFF_BUDGET = 1_200_000
#: Longest run of oracle output kept per probe tail.
MAX_TAIL_LINES = 40

SOURCE_LABELS = {
    TraceSource.ALAN_CODE: "Alan Code via OpenCode",
    TraceSource.CLAUDE_CODE: "Claude Code",
    TraceSource.ALAN: "own harness",
    TraceSource.SYNTHETIC: "synthetic",
}

#: The label and note that always travel with model-written text. They live here, not in the page
#: script, so a page without an explanation holds neither.
AI_LABEL = "AI explanation"
AI_NOTE = (
    "Written by a language model to help you read the diff. "
    "It does not affect the verdict, and it can be wrong."
)

_STAMP = re.compile(r"-\d{8}-\d{6}$")


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


# --------------------------------------------------------------------------- text helpers


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> tuple[str, bool]:
    """Keep the start and the end of a long text: the end of test output is what matters."""
    if len(text) <= limit:
        return text, False
    head = limit * 5 // 8
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n... {cut} characters clipped ...\n{text[-tail:]}", True


def _tail_lines(text: str, limit: int = MAX_TAIL_LINES) -> str:
    lines = strip_ansi(text).rstrip().splitlines()
    if len(lines) > limit:
        lines = [f"... {len(lines) - limit} earlier lines left out", *lines[-limit:]]
    return _clip("\n".join(lines))[0]


def _args_pairs(args: dict[str, Any]) -> tuple[list[list[str]], bool]:
    """Arguments as ``[name, text]`` pairs, long values clipped, total size bounded."""
    pairs: list[list[str]] = []
    clipped = False
    used = 0
    for key, value in (args or {}).items():
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        budget = max(MAX_FIELD_CHARS - used, 200)
        text, c = _clip(text, min(budget, 2500))
        clipped = clipped or c
        used += len(text)
        pairs.append([str(key), text])
    return pairs, clipped


# --------------------------------------------------------------------------- steps and diffs


def _diff_text(run_dir: Path, trace: Trace, step: Step) -> str:
    """Unified diff from the state before ``step`` to the state after it, or ``""``."""
    if step.snapshot is None:
        return ""
    before = trace.snapshot_at(step.index - 1)
    if before == step.snapshot:
        return ""
    try:
        from agentbisect import snapshot

        return snapshot.diff(run_dir, before, step.snapshot)
    except Exception:  # shadow repo missing or damaged: the page still works without diffs
        return ""


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit], True


def _step_data(
    trace: Trace,
    run_dir: Path,
    shorten: Shortener,
    priority: dict[int, str],
) -> list[dict[str, Any]]:
    """One dict per step. ``priority`` maps step -> a stored diff for steps the verdict names."""
    steps = trace.steps
    order = sorted(range(len(steps)), key=lambda k: (steps[k].index not in priority, k))
    diffs: dict[int, tuple[str | None, bool, bool]] = {}  # index -> (text, truncated, skipped)
    used = 0
    for k in order:
        step = steps[k]
        if step.kind not in MUTATING_KINDS:
            continue
        cap = MAX_CULPRIT_DIFF if step.index in priority else MAX_STEP_DIFF
        text = priority.get(step.index) or _diff_text(run_dir, trace, step)
        text, trunc = _cut(text, cap)
        if used + len(text) > DIFF_BUDGET and step.index not in priority:
            diffs[step.index] = (None, False, True)
            continue
        used += len(text)
        diffs[step.index] = (text, trunc, False)

    out: list[dict[str, Any]] = []
    for step in steps:
        args, c1 = _args_pairs(step.args)
        output, c2 = _clip(strip_ansi(step.output or ""))
        reasoning, c3 = _clip(step.reasoning or "", MAX_REASONING_CHARS)
        entry: dict[str, Any] = {
            "i": step.index,
            "kind": step.kind.value,
            "tool": step.tool,
            "label": step_label(step, shorten),
            "mut": step.kind in MUTATING_KINDS,
            "tokens": step.tokens,
            "exit": step.exit_code,
            "why": reasoning,
            "args": args,
            "out": output,
            "clipped": c1 or c2 or c3,
            "snap": step.snapshot[:10] if step.snapshot else None,
        }
        if step.index in diffs:
            text, trunc, skipped = diffs[step.index]
            entry["diff"] = text
            entry["diff_truncated"] = trunc
            entry["diff_skipped"] = skipped
        out.append(entry)
    return out


# --------------------------------------------------------------------------- replay


def _frames(
    trace: Trace,
    probes: list[tuple[int, str, int | None, int | None, str]],
    *,
    shaded: bool,
) -> list[dict[str, Any]]:
    """One frame per probe, with the interval ``(lo, hi)`` that is still unknown after it.

    ``lo`` is the last step known good and ``hi`` the first known bad, both widened over steps
    that share a snapshot with the probed one (a step that changed no file has the same state).
    ``lo = -1`` and ``hi = n + 1`` mean nothing is known on that side. A history that is not
    monotone has no shrinking interval, so ``lo`` and ``hi`` are ``None`` there.
    """
    n = trace.n

    def widen_up(s: int) -> int:
        while s + 1 <= n and trace.snapshot_at(s + 1) == trace.snapshot_at(s):
            s += 1
        return s

    def widen_down(s: int) -> int:
        while s - 1 >= 0 and trace.snapshot_at(s - 1) == trace.snapshot_at(s):
            s -= 1
        return s

    lo, hi = -1, n + 1
    frames: list[dict[str, Any]] = []
    for k, (step, verdict, runs, fails, role) in enumerate(probes, 1):
        if shaded:
            if verdict == Verdict.PASS.value:
                lo = max(lo, widen_up(step))
            elif verdict == Verdict.FAIL.value:
                hi = min(hi, widen_down(step))
        frames.append(
            {
                "n": k,
                "step": step,
                "verdict": verdict,
                "runs": runs,
                "fails": fails,
                "role": role,
                "lo": lo if shaded else None,
                "hi": hi if shaded else None,
            }
        )
    return frames


def _real_replay(trace: Trace, b: BisectResult) -> dict[str, Any]:
    probes = [
        (
            p.step_index,
            p.verdict.value,
            p.n_runs,
            p.n_fail,
            "endpoint" if i <= b.precondition_probes else "search",
        )
        for i, p in enumerate(b.probes, 1)
    ]
    mono = not b.non_monotone
    return {
        "frames": _frames(trace, probes, shaded=mono),
        "mono": mono,
        "reconstructed": False,
        "window_end": trace.n,
    }


def _reconstructed_replay(trace: Trace, t_err: int, seen_at: int) -> dict[str, Any]:
    """The probes a binary search over steps ``0..seen_at`` makes when the answer is ``t_err``.

    The probe log of a repaired break is not stored, so this is an illustration: it takes the
    known answer and halves the window the way ``bisect`` does, over steps whose snapshot differs
    from the step before. The page labels it as reconstructed.
    """
    k = min(seen_at, trace.n)
    cand = [0] + [j for j in range(1, k + 1) if trace.snapshot_at(j) != trace.snapshot_at(j - 1)]
    m = len(cand) - 1
    first_bad = next((idx for idx, c in enumerate(cand) if c >= t_err), m)

    def verdict(idx: int) -> str:
        return Verdict.FAIL.value if idx >= first_bad else Verdict.PASS.value

    order = [(0, "endpoint"), (m, "endpoint")]
    lo, hi = 0, m
    while hi - lo > 1:
        mid = (lo + hi) // 2
        order.append((mid, "search"))
        if verdict(mid) == Verdict.PASS.value:
            lo = mid
        else:
            hi = mid
    probes = [(cand[idx], verdict(idx), None, None, role) for idx, role in order]
    return {
        "frames": _frames(trace, probes, shaded=True),
        "mono": True,
        "reconstructed": True,
        "window_end": k,
    }


# --------------------------------------------------------------------------- verdict parts


def _probe_rows(culprit: CulpritReport) -> list[dict[str, Any]]:
    b = culprit.bisect
    return [
        {
            "n": i,
            "step": p.step_index,
            "role": "endpoint" if i <= b.precondition_probes else "search",
            "verdict": p.verdict.value,
            "runs": p.n_runs,
            "fails": p.n_fail,
            "seconds": round(sum(r.duration_s for r in p.runs), 2),
        }
        for i, p in enumerate(b.probes, 1)
    ]


def _oracle_tail(culprit: CulpritReport) -> tuple[str | None, int | None]:
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
    return _tail_lines(run.output_tail), pick.step_index


def _analysis_data(culprit: CulpritReport) -> dict[str, Any] | None:
    a = culprit.analysis
    if a is None:
        return None
    return {
        "t_err": a.t_err,
        "t_obs": a.t_obs,
        "lag": a.lag,
        "censored": a.censored or a.t_obs is None,
        "after": a.steps_after_err,
        "no_opportunity": a.no_opportunity,
        "wasted": a.wasted_tokens,
        "wasted_fmt": fmt_tokens(a.wasted_tokens),
        "total": a.total_tokens,
        "share_pct": round(a.wasted_share * 100, 1),
        "share_fmt": fmt_percent(a.wasted_share),
        "signal": (a.observed_signal or "").strip().strip("_= ").strip() or a.observed_signal,
    }


def _confidence(b: BisectResult) -> str | None:
    if b.confidence is None:
        return None
    return f"{b.confidence * 100:.1f}%" if b.confidence < 0.999 else ">99.9%"


def _stats(
    decisive: int, an: dict[str, Any] | None, confidence: str | None
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = [{"k": "Decisive step", "v": str(decisive)}]
    if an is not None:
        censored = an["censored"]
        last = censored and an["after"] == 0
        cards.append(
            {"k": "Noticed at", "v": ("n/a" if last else "never") if censored else str(an["t_obs"])}
        )
        if last:
            cards.append({"k": "Detection lag", "v": "n/a", "small": "(last step)"})
        elif censored:
            cards.append(
                {"k": "Detection lag", "v": "censored", "small": f"(≥ {an['after']} steps)"}
            )
        else:
            lag = an["lag"]
            cards.append(
                {"k": "Detection lag", "v": str(lag), "small": "step" if lag == 1 else "steps"}
            )
        cards.append(
            {
                "k": "Wasted tokens",
                "v": an["wasted_fmt"],
                "small": f"{an['share_fmt']} of run",
                "bar": an["share_pct"],
                "title": f"{an['wasted']:,}",
            }
        )
    if confidence:
        cards.append({"k": "Confidence", "v": confidence})
    return cards


def _member(n: int, culprit: CulpritReport, trace: Trace, shorten: Shortener) -> dict[str, Any]:
    from agentbisect.report.text import verdict_sentences

    b = culprit.bisect
    found = b.status is BisectStatus.FOUND and b.decisive_step is not None
    t = b.decisive_step
    step = None
    if found and t is not None and 1 <= t <= trace.n:
        step = culprit.step or trace.steps[t - 1]
    tail, tail_step = _oracle_tail(culprit)
    an = _analysis_data(culprit) if found else None
    conf = _confidence(b)
    gt = trace.meta.sabotage_step
    m: dict[str, Any] = {
        "id": f"culprit-{n}",
        "check": culprit.check,
        "label": check_label(culprit.check),
        "found": found,
        "reason": b.reason,
        "decisive": t,
        "sentences": verdict_sentences(culprit, step, shorten) if found else [],
        "analysis": an,
        "stats": _stats(t, an, conf) if found and t is not None else [],
        "confidence": conf,
        "non_monotone": b.non_monotone,
        "transitions": list(b.transitions),
        "policy_rule": b.policy.rule if b.policy else None,
        "flake_rate": f"{b.policy.flake_rate:.2f}" if b.policy else None,
        "oracle_tail": tail,
        "oracle_tail_step": tail_step,
        "probes": _probe_rows(culprit),
        "replay": _real_replay(trace, b),
        "explanation": culprit.explanation,  # raw text; the group carries it with its label
        "ground_truth": None,
        "ground_truth_ok": None,
    }
    if found and gt is not None:
        m["ground_truth"] = f"Sabotage was injected at step {gt}."
        m["ground_truth_ok"] = gt == t
    return m


def _group(
    group: list[CulpritReport],
    members: list[dict[str, Any]],
    trace: Trace,
    shorten: Shortener,
) -> dict[str, Any]:
    primary = members[0]
    t = primary["decisive"]
    sentences: list[str] = []
    if primary["found"] and t is not None:
        step = group[0].step or (trace.steps[t - 1] if 1 <= t <= trace.n else None)
        sentences = group_verdict_sentences(group, step, shorten)
    pairs = {tuple((f["step"], f["verdict"]) for f in m["replay"]["frames"]) for m in members}
    text = next((m["explanation"] for m in members if m["explanation"]), None)
    return {
        "type": "group",
        "id": primary["id"],
        "found": primary["found"],
        "decisive": t,
        "label": primary["label"] if len(members) == 1 else f"{len(members)} tests",
        "sentences": sentences,
        "shared": shares_analysis(group),
        "same_probes": len(pairs) == 1,
        "explanation": {"label": AI_LABEL, "note": AI_NOTE, "text": text} if text else None,
        "members": members,
    }


def _episode(n: int, ep: RepairEpisode, trace: Trace, shorten: Shortener) -> dict[str, Any]:
    t = ep.t_err
    step = trace.steps[t - 1] if t is not None and 1 <= t <= trace.n else None
    seen_out = None
    if 1 <= ep.seen_at <= trace.n:
        out = trace.steps[ep.seen_at - 1].output or ""
        seen_out = _tail_lines(out) if out.strip() else None
    total = trace.total_tokens
    an = None
    stats: list[dict[str, Any]] = []
    replay = None
    if t is not None:
        lag = ep.lag if ep.lag is not None else ep.seen_at - t
        wasted = ep.wasted_tokens
        an = {
            "t_err": t,
            "t_obs": ep.seen_at,
            "lag": lag,
            "censored": False,
            "after": trace.n - t,
            "no_opportunity": False,
            "wasted": wasted,
            "wasted_fmt": fmt_tokens(wasted) if wasted is not None else None,
            "total": total,
            "share_pct": round(wasted / total * 100, 1) if wasted is not None and total else None,
            "share_fmt": fmt_percent(wasted / total) if wasted is not None and total else None,
            "signal": None,
        }
        stats = [
            {"k": "Broke at step", "v": str(t)},
            {"k": "Seen at step", "v": str(ep.seen_at)},
            {"k": "Detection lag", "v": str(lag), "small": "step" if lag == 1 else "steps"},
        ]
        if wasted is not None:
            stats.append(
                {"k": "Tokens in broken code", "v": fmt_tokens(wasted), "title": f"{wasted:,}"}
            )
        replay = _reconstructed_replay(trace, t, ep.seen_at)
    else:
        stats = [
            {"k": "Broke at step", "v": "not found"},
            {"k": "Seen at step", "v": str(ep.seen_at)},
        ]
    return {
        "type": "episode",
        "id": f"repaired-{n}",
        "found": t is not None,
        "check": ep.check,
        "label": check_label(ep.check),
        "decisive": t,
        "seen_at": ep.seen_at,
        "reason": ep.reason,
        "sentences": repaired_sentences(ep, step, shorten),
        "analysis": an,
        "stats": stats,
        "replay": replay,
        "seen_output": seen_out,
    }


def _overview(
    has_bisect: bool, auto_oracle: bool, n_culprits: int, n_episodes: int
) -> dict[str, str] | None:
    """The banner for a run with nothing to bisect, or ``None`` when a verdict follows."""
    if not has_bisect:
        return {
            "kind": "warn",
            "title": "Not bisected yet",
            "text": "No bisect.json in this run. Run `agentbisect run --trace <run dir>` first, "
            "then build this report again.",
        }
    if n_culprits:
        return None
    if n_episodes:
        title = (
            "The agent broke a test and fixed it"
            if n_episodes == 1
            else f"The agent broke {n_episodes} tests and fixed them"
        )
        return {
            "kind": "ok",
            "title": title,
            "text": "The run ends with no failing check. Details are in the tab below.",
        }
    text = "The bisect finished without a failing check to trace."
    if auto_oracle:
        text += " Auto-oracle found no test that passed at the start and fails at the end."
    return {"kind": "warn", "title": "No culprit found", "text": text}


# --------------------------------------------------------------------------- entry points


def _load(run_dir: Path) -> tuple[Trace, RunReport | None, Shortener]:
    trace = Trace.load(run_dir)
    bisect_path = run_dir / "bisect.json"
    report = (
        RunReport.model_validate_json(bisect_path.read_text()) if bisect_path.exists() else None
    )
    return trace, report, Shortener.for_run(trace.meta, run_dir)


def build_data(run_dir: Path) -> dict[str, Any]:
    """Everything the run page draws, as JSON-ready data."""
    run_dir = Path(run_dir)
    trace, report, shorten = _load(run_dir)
    m = trace.meta
    culprits = report.culprits if report else []
    episodes = report.repaired if report else []

    members = {id(c): _member(i + 1, c, trace, shorten) for i, c in enumerate(culprits)}
    groups = [
        _group(group, [members[id(c)] for c in group], trace, shorten)
        for group in group_by_step(culprits)
    ]
    eps = [_episode(i + 1, e, trace, shorten) for i, e in enumerate(episodes)]

    # Steps the verdict is about get their diff first and a larger cap.
    priority: dict[int, str] = {}
    for c in culprits:
        t = c.bisect.decisive_step
        if c.bisect.status is BisectStatus.FOUND and t is not None and c.diff:
            priority.setdefault(t, c.diff)
    for e in eps:
        if e["decisive"] is not None:
            priority.setdefault(e["decisive"], "")
    for g in groups:
        if g["decisive"] is not None:
            priority.setdefault(g["decisive"], "")

    return {
        "page": "run",
        "run": {
            "id": m.run_id,
            "repo": m.repo_origin,
            "task": None if m.repo_origin else m.task_id,
            "model": model_label(m.model),
            "source": SOURCE_LABELS.get(m.source, m.source.value),
            "n_steps": trace.n,
            "n_mutating": sum(1 for s in trace.steps if s.kind in MUTATING_KINDS),
            "tokens": fmt_tokens(trace.total_tokens),
            "tokens_exact": f"{trace.total_tokens:,}",
            "cost": f"${m.cost_usd:.4f}" if m.cost_usd else None,
            "created": m.created_at.strftime("%Y-%m-%d %H:%M UTC"),
            "sabotage_step": m.sabotage_step,
        },
        "has_bisect": report is not None,
        "auto_oracle": bool(report and report.auto_oracle),
        "overview": _overview(
            report is not None, bool(report and report.auto_oracle), len(culprits), len(eps)
        ),
        "n_checks": len(culprits),
        "n_found": sum(1 for g in groups if g["found"]),
        "groups": groups,
        "episodes": eps,
        "steps": _step_data(trace, run_dir, shorten, priority),
    }


def run_headline(run_dir: Path) -> dict[str, Any]:
    """A short card for a run: tag, verdict sentences, size. Used by the study landing page."""
    run_dir = Path(run_dir)
    trace, report, shorten = _load(run_dir)
    m = trace.meta
    culprits = report.culprits if report else []
    episodes = report.repaired if report else []
    sentences: list[str] = []
    tag, kind, rank = "Not bisected yet", "warn", 3
    groups = group_by_step(culprits)
    if groups and groups[0][0].bisect.status is BisectStatus.FOUND:
        first = groups[0]
        t = first[0].bisect.decisive_step
        step = first[0].step or (trace.steps[t - 1] if t and 1 <= t <= trace.n else None)
        sentences = group_verdict_sentences(first, step, shorten)
        tag, kind, rank = "Culprit found", "bad", 0
        if m.sabotage_step is not None:
            hit = any(c.bisect.decisive_step == m.sabotage_step for c in culprits)
            tag = "Planted break found" if hit else "Planted break missed"
    elif episodes:
        ep = episodes[0]
        t = ep.t_err
        step = trace.steps[t - 1] if t is not None and 1 <= t <= trace.n else None
        sentences = repaired_sentences(ep, step, shorten)
        tag, kind, rank = "Caught and fixed", "ok", 1
    elif report is not None:
        sentences = [
            "No test that passed at the start fails at the end.",
            f"The agent worked for {trace.n} steps and the run ended green.",
        ]
        tag, kind, rank = "Ended green", "ok", 2
    return {
        "run_id": m.run_id,
        "tag": tag,
        "kind": kind,
        "rank": rank,
        "sentences": sentences,
        "n_steps": trace.n,
        "tokens": fmt_tokens(trace.total_tokens),
        "model": m.model,
        "repo": m.repo_origin,
        "has_bisect": report is not None,
    }


def is_stamped(run_id: str) -> bool:
    """True for ids that ``record`` made (``<task>-<model>-YYYYMMDD-HHMMSS``)."""
    return bool(_STAMP.search(run_id))


def render_report(run_dir: Path) -> Path:
    """Write ``run_dir/report.html`` and return its path."""
    run_dir = Path(run_dir)
    data = build_data(run_dir)
    run = data["run"]
    html = render_page(
        "run.html.j2",
        title=f"AgentBisect report {run['id']}",
        data=data,
        app=APP,
        styles=STYLES,
    )
    out = run_dir / "report.html"
    write_text_stable(out, html)
    return out
