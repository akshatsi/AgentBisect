"""The study: turn the recorded Alan Code runs into rows, repaired-break episodes and metrics.

Follows docs/METHODS.md §5-§8, variant B (DeepSeek V4.1 Flash only). Three public functions:

* ``collect(runs_dir)``  scans ``runs/*/meta.json``, drops runs that are not study data (with a
  reason), bisects the rest, and returns a ``StudySummary`` with rows, exclusions and episodes.
* ``summarize(summary)`` fills ``metrics`` from the rows and episodes, using ``stats`` only.
* ``write(summary, out_dir)`` saves ``study.json``.

Nothing here decides pass or fail except the oracle (``bisect.py``); no LLM is involved.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from agentbisect import stats
from agentbisect.bisect import (
    SUITE_CMD,
    bisect_run,
    find_episodes,
)
from agentbisect.hooks import opencode
from agentbisect.models import (
    BisectStatus,
    Metric,
    RepairEpisode,
    RunReport,
    StudyRow,
    StudySummary,
    Trace,
)

__all__ = ["collect", "summarize", "write", "find_episodes", "DESIGN"]

DESIGN = (
    "METHODS §8 variant B: DeepSeek V4.1 Flash only in Alan Code, the 10 t-tasks plus 6 repeats "
    "(16 task runs) and 3 sabotage runs; sabotage runs count for accuracy, not for lag."
)

#: METHODS §8.4: a pooled Kaplan-Meier median needs this many bisectable broken runs, of which
#: at least ``KM_MIN_OBSERVED`` were observed.
KM_MIN_RUNS = 6
KM_MIN_OBSERVED = 3

STUDY_DIRNAME = "study"
_SKIP_DIRS = {STUDY_DIRNAME}


# =========================================================================== collect


def collect(runs_dir: Path, *, rebisect: bool = False, timeout_s: float = 120.0) -> StudySummary:
    """Scan ``runs_dir`` and build the study rows, exclusions and repaired-break episodes.

    A run is study data if it is a finished Alan Code run of DeepSeek V4.1 Flash with per-step
    snapshots, and it is not a resumed conversation. Every other directory with a ``meta.json``
    lands in ``StudySummary.excluded`` (keyed by directory name) with the reason. Each kept run is
    finalized (when it has ``events.jsonl``) and bisected with the auto-oracle; the result is
    cached in ``bisect.json``, so ``rebisect=True`` is needed to redo it.
    """
    runs_dir = Path(runs_dir)
    summary = StudySummary(design=DESIGN)
    kept: list[tuple[str, Path, Trace]] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        name = run_dir.name
        if name.startswith(".") or name in _SKIP_DIRS:
            continue
        trace, reason = _load_study_run(run_dir)
        if reason is not None:
            summary.excluded[name] = reason
        elif trace is not None:
            kept.append((name, run_dir, trace))
    kept.sort(key=lambda item: (item[2].meta.created_at, item[0]))

    cache_dir = runs_dir / STUDY_DIRNAME / "cache"
    for name, run_dir, trace in kept:
        report = _bisect_report(trace, run_dir, rebisect, timeout_s)
        fault = _step0_fault(report)
        if fault is not None:
            summary.excluded[name] = f"suite cannot run at step 0 (environment fault): {fault}"
            continue
        summary.rows.extend(_rows(name, trace, report))
        summary.episodes.extend(
            _cached_episodes(name, trace, run_dir, cache_dir, rebisect, timeout_s)
        )
    return summary


def _load_study_run(run_dir: Path) -> tuple[Trace | None, str | None]:
    """``(trace, None)`` for study data, else ``(None, reason)``."""
    name = run_dir.name
    if name.startswith("x-"):
        return None, "marked as discarded (directory name starts with 'x-')"
    if not (run_dir / "meta.json").exists():
        return None, "no meta.json (not a run)"
    try:
        trace = Trace.load(run_dir)
    except (OSError, ValueError) as exc:
        return None, f"trace could not be loaded: {exc.__class__.__name__}"
    source = trace.meta.source.value
    if source != "alan_code":
        return None, f"not an Alan Code run (source {source}): synthetic, fake or demo run"
    try:
        if (run_dir / "events.jsonl").exists():
            # The agent may never have gone idle, so merge the events into the trace first.
            summary = opencode.finalize_run(run_dir)
            trace = Trace.load(run_dir)
        else:
            summary = _read_json(run_dir / "finalize.json")
    except (OSError, ValueError) as exc:
        return None, f"finalize failed: {exc.__class__.__name__}"
    if summary.get("resumed_conversation") or opencode._resumed(run_dir):
        return None, "Alan resumed an earlier conversation into this run (contaminated)"
    if summary.get("mode") == "events_only" or summary.get("per_step_snapshots") is False:
        return None, "events-only run: no per-step snapshots, so it cannot be bisected"
    if not trace.meta.finished:
        return None, "unfinished run (the agent has not gone idle yet)"
    if trace.n == 0:
        return None, "trace has no steps"
    if "flash" not in (trace.meta.model or "").lower():
        return None, f"model {trace.meta.model!r} is not DeepSeek V4.1 Flash"
    if trace.meta.task_id is None and trace.meta.sabotage_step is None:
        # Own-repo runs differ in code and task, so they are case studies. A planted break on one
        # still tests the bisector, so those runs stay and count only toward sabotage accuracy.
        return None, "own-repo run without a planted break: a case study, not study data"
    return trace, None


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def _bisect_report(trace: Trace, run_dir: Path, rebisect: bool, timeout_s: float) -> RunReport:
    """The auto-oracle ``RunReport`` for a run: from ``bisect.json`` or freshly bisected."""
    path = run_dir / "bisect.json"
    if path.exists() and not rebisect:
        try:
            cached = RunReport.model_validate_json(path.read_text())
        except ValueError:
            cached = None
        if cached is not None and cached.auto_oracle:  # a hand-picked --check is not the study
            return cached
    report = bisect_run(trace, run_dir, None, timeout_s)
    path.write_text(report.model_dump_json(indent=2))  # exactly what `agentbisect run` writes
    return report


#: Reasons from ``bisect`` that mean the whole suite could not run on the base snapshot.
_STEP0_PREFIXES = ("full suite could not run at step 0", "oracle error at step 0")


def _step0_fault(report: RunReport) -> str | None:
    """The reason if the whole suite could not run at step 0 (METHODS §8.4), else None.

    That is a problem with the environment, not the agent, so the run is not study data. A suite
    that fails to run only at the final step is the agent's doing and stays in as broken.
    """
    for c in report.culprits:
        reason = c.bisect.reason or ""
        if (
            c.check == SUITE_CMD
            and c.bisect.status is BisectStatus.NOT_BISECTABLE
            and reason.startswith(_STEP0_PREFIXES)
        ):
            return reason
    return None


def _rows(name: str, trace: Trace, report: RunReport) -> list[StudyRow]:
    """One row per culprit; one row for a run with none. See ``StudyRow``."""
    meta = trace.meta
    common = {
        "run_id": name,
        "task_id": meta.task_id or "unknown",
        "model": meta.model or "unknown",
        "sabotage_step": meta.sabotage_step,
        "n_steps": trace.n,
        "total_tokens": trace.total_tokens,
        "cost_usd": meta.cost_usd,
    }
    if not report.culprits:
        return [StudyRow(**common, final_failed=False, bisectable=False)]
    rows = []
    for c in report.culprits:
        found = c.bisect.status is BisectStatus.FOUND and c.analysis is not None
        a = c.analysis
        rows.append(
            StudyRow(
                **common,
                final_failed=True,
                bisectable=found,
                check=c.check,
                t_err=a.t_err if found and a else None,
                t_obs=a.t_obs if found and a else None,
                lag=a.lag if found and a else None,
                censored=a.censored if found and a else None,
                steps_after_err=a.steps_after_err if found and a else None,
                wasted_tokens=a.wasted_tokens if found and a else None,
                sabotage_correct=(
                    None
                    if meta.sabotage_step is None
                    else bool(found and a and a.t_err == meta.sabotage_step)
                ),
                no_opportunity=bool(found and a and a.no_opportunity),
            )
        )
    return rows


# =========================================================================== repaired-break episodes
# The detection and bisecting live in bisect.py (``find_episodes``); the study caches them.


def _cached_episodes(
    name: str, trace: Trace, run_dir: Path, cache_dir: Path, rebisect: bool, timeout_s: float
) -> list[RepairEpisode]:
    """``find_episodes`` with a cache in ``runs/study/cache/``, keyed by the trace content."""
    digest = hashlib.sha256()
    digest.update((run_dir / "trace.jsonl").read_bytes())
    digest.update(trace.meta.base_snapshot.encode())
    digest.update(b"episodes-v2")  # v2: sabotage runs are searched too
    key = digest.hexdigest()
    path = cache_dir / f"{name}.episodes.json"
    if path.exists() and not rebisect:
        try:
            data = json.loads(path.read_text())
            if data.get("key") == key:
                return [RepairEpisode.model_validate(e) for e in data["episodes"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    episodes = find_episodes(trace, run_dir, timeout_s)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"key": key, "episodes": [e.model_dump(mode="json") for e in episodes]}, indent=1
        )
    )
    return episodes


# =========================================================================== summarize


def summarize(summary: StudySummary) -> StudySummary:
    """Fill ``summary.metrics`` from its rows and episodes (METHODS §5-§8). Returns ``summary``."""
    rows = summary.rows
    task_rows = [r for r in rows if r.sabotage_step is None]
    sabotage_rows = [r for r in rows if r.sabotage_step is not None]
    task_runs = _by_run(task_rows)
    sabotage_runs = _by_run(sabotage_rows)

    metrics: list[Metric] = []
    n_all = len({r.run_id for r in rows})
    metrics.append(
        Metric(
            name="Runs",
            value=float(n_all),
            n=n_all,
            method="count",
            note=f"{len(task_runs)} task runs, {len(sabotage_runs)} sabotage runs "
            f"({len(summary.excluded)} directories excluded)",
        )
    )

    broken = {rid: rs for rid, rs in task_runs.items() if any(r.final_failed for r in rs)}
    metrics.append(
        _proportion(
            "Broken-run share",
            len(broken),
            len(task_runs),
            "Wilson 95%",
            "runs whose final state fails the oracle / task runs (sabotage runs excluded)",
        )
    )
    found_runs = {rid: rs for rid, rs in broken.items() if any(r.bisectable for r in rs)}
    metrics.append(
        _proportion(
            "Bisectable share of broken runs",
            len(found_runs),
            len(broken),
            "Wilson 95%",
            "broken runs with at least one culprit found / broken runs",
        )
    )

    # Runs whose break came on the last step had no chance to be seen (METHODS §4.4). They stay in
    # the shares above and in sabotage accuracy, and drop out of everything about lag.
    primary_all = _primary_rows(task_rows)
    primary = [r for r in primary_all if not r.no_opportunity]
    n_no_opp = len(primary_all) - len(primary)
    n_sab_no_opp = len(
        [r for r in _primary_rows(sabotage_rows) if r.no_opportunity],
    )
    n_cens = sum(1 for r in primary if r.censored)
    metrics.append(
        _proportion(
            "Censoring rate of broken runs",
            n_cens,
            len(primary),
            "Wilson 95%",
            "runs where the agent never saw the failure / bisectable broken runs",
        )
    )
    metrics.append(
        _proportion(
            "Noticed at once (lag 0)",
            sum(1 for r in primary if r.lag == 0),
            len(primary),
            "Wilson 95%",
            "bisectable broken runs where the failing step itself showed the failure "
            "(1 - S(0), METHODS §5.2)",
        )
    )
    metrics.append(_km_metric(primary))
    metrics.extend(_wasted_metrics(primary))
    metrics.append(
        Metric(
            name="No opportunity to observe (break on last step)",
            value=float(n_no_opp),
            n=len(primary_all),
            method="count",
            note=f"{n_no_opp} of {len(primary_all)} bisectable broken task runs; left out of "
            "the censoring rate, the Kaplan-Meier curve and the wasted-token share "
            f"(METHODS §4.4). Sabotage runs with this flag: {n_sab_no_opp}.",
        )
    )

    # A sabotage run is judged on the final state, or on a break the agent saw: an episode
    # whose decisive step is the injected one shows the break was live where it was detected.
    sab_episodes: dict[str, list[RepairEpisode]] = defaultdict(list)
    for e in summary.episodes:
        if e.run_id in sabotage_runs:
            sab_episodes[e.run_id].append(e)
    sab_rated = {
        rid: rs
        for rid, rs in sabotage_runs.items()
        if any(r.final_failed for r in rs) or sab_episodes.get(rid)
    }
    sab_ok = 0
    for rid, rs in sab_rated.items():
        injected = rs[0].sabotage_step
        by_final = any(r.bisectable and r.sabotage_correct for r in rs)
        by_episode = any(e.t_err == injected for e in sab_episodes.get(rid, []))
        sab_ok += by_final or by_episode
    metrics.append(
        _proportion(
            "Sabotage accuracy",
            sab_ok,
            len(sab_rated),
            "Wilson 95%",
            "sabotage runs where the final-state culprit or a break the agent saw has the "
            "injected step as its decisive step / sabotage runs that fail at the end or show "
            "a seen break (not bisectable counts as wrong)",
        )
    )
    metrics.extend(_episode_metrics([e for e in summary.episodes if e.run_id in task_runs]))
    summary.metrics = metrics
    summary.design = summary.design or DESIGN
    return summary


def _by_run(rows: list[StudyRow]) -> dict[str, list[StudyRow]]:
    out: dict[str, list[StudyRow]] = {}
    for r in rows:
        out.setdefault(r.run_id, []).append(r)
    return out


def _proportion(name: str, k: int, n: int, method: str, note: str) -> Metric:
    if n == 0:
        return Metric(name=name, value=None, n=0, method=method, note=f"no data: {note}")
    lo, hi = stats.wilson_interval(k, n)
    return Metric(
        name=name, value=k / n, ci_low=lo, ci_high=hi, n=n, method=method, note=f"{k}/{n}: {note}"
    )


def _primary_rows(task_rows: list[StudyRow]) -> list[StudyRow]:
    """One FOUND row per run for the lag analysis (METHODS §5.1).

    The culprit with the smallest ``t_err``; ties go to the smallest ``t_obs`` (censored counts as
    infinite), then to the check string.
    """
    out = []
    for rs in _by_run([r for r in task_rows if r.bisectable and r.t_err is not None]).values():
        out.append(
            min(
                rs,
                key=lambda r: (
                    r.t_err,
                    math.inf if r.censored or r.t_obs is None else r.t_obs,
                    r.check or "",
                ),
            )
        )
    return out


def _km_metric(primary: list[StudyRow]) -> Metric:
    name = "Detection lag, Kaplan-Meier median (steps)"
    method = "Kaplan-Meier, Greenwood log-log 95% band (METHODS §5)"
    n_obs = sum(1 for r in primary if not r.censored)
    if len(primary) < KM_MIN_RUNS or n_obs < KM_MIN_OBSERVED:
        return Metric(
            name=name,
            value=None,
            n=len(primary),
            method=method,
            note=(
                f"not estimated: {len(primary)} bisectable broken runs ({n_obs} observed); "
                f"METHODS §8.4 needs at least {KM_MIN_RUNS} and {KM_MIN_OBSERVED} observed. "
                "Each run is listed as a row instead."
            ),
        )
    durations = [float(r.steps_after_err if r.censored else r.lag) for r in primary]  # type: ignore[arg-type]
    observed = [not r.censored for r in primary]
    med = stats.km_median_ci(durations, observed)
    note = f"{med.events} observed of {med.n}"
    if med.median is None:
        note += "; median not reached (fewer than half the agents noticed)"
    note += (
        ". Censoring is not independent of lag: exposure is short when the break comes late "
        "(METHODS §5.4)."
    )
    return Metric(
        name=name,
        value=med.median,
        ci_low=med.lower,
        ci_high=med.upper,
        n=med.n,
        method=method,
        note=note,
    )


def _wasted_metrics(primary: list[StudyRow]) -> list[Metric]:
    name = "Wasted-token share (pooled)"
    if not primary:
        return [
            Metric(name=name, value=None, n=0, method="pooled ratio", note="no bisectable run"),
            Metric(
                name="Wasted-token share (median per run)",
                value=None,
                n=0,
                method="median",
                note="no bisectable run",
            ),
        ]

    def pooled(rs: list[StudyRow]) -> float:
        total = sum(r.total_tokens for r in rs)
        return sum(r.wasted_tokens or 0 for r in rs) / total if total else 0.0

    by_task: dict[str, list[StudyRow]] = defaultdict(list)
    for r in primary:
        by_task[r.task_id].append(r)
    groups = list(by_task.values())
    lo, hi = stats.bootstrap_ci(groups, lambda gs: pooled([r for g in gs for r in g]))
    note = f"sum wasted / sum total tokens; cluster bootstrap over {len(groups)} task(s), B=2000, seed 0"
    if len(groups) < 2:
        note += " (one task only: the interval is not informative)"
    shares = [
        r.wasted_tokens / r.total_tokens
        for r in primary
        if r.total_tokens and r.wasted_tokens is not None
    ]
    median = Metric(
        name="Wasted-token share (median per run)",
        value=statistics.median(shares) if shares else None,
        n=len(shares),
        method="median of per-run shares, no interval (METHODS §6.2)",
    )
    return [
        Metric(
            name=name,
            value=pooled(primary),
            ci_low=lo,
            ci_high=hi,
            n=len(primary),
            method="pooled ratio, cluster bootstrap percentile 95%",
            note=note,
        ),
        median,
    ]


def _episode_metrics(episodes: list[RepairEpisode]) -> list[Metric]:
    done = [e for e in episodes if e.lag is not None]
    skipped = len(episodes) - len(done)
    out = [
        Metric(
            name="Repaired-break episodes",
            value=float(len(done)),
            n=len(done),
            method="count (METHODS §8.4)",
            note=f"{skipped} more could not be bisected or were injected" if skipped else None,
        )
    ]
    name = "Repaired-break lag, median (steps)"
    method = "median over episodes; cluster bootstrap over runs, B=2000, seed 0 (METHODS §8.4)"
    if not done:
        out.append(Metric(name=name, value=None, n=0, method=method, note="no episodes"))
        return out
    lags = [float(e.lag) for e in done]  # type: ignore[arg-type]
    per_run: dict[str, list[float]] = defaultdict(list)
    for e in done:
        per_run[e.run_id].append(float(e.lag))  # type: ignore[arg-type]
    items = list(per_run.values())
    stat: Callable[[list[list[float]]], float] = lambda rs: statistics.median(  # noqa: E731
        [x for r in rs for x in r]
    )
    lo, hi = stats.bootstrap_ci(items, stat)
    note = (
        f"{len(done)} episodes in {len(items)} run(s); never censored by construction, "
        "so they say nothing about breaks the agent did not see"
    )
    if len(items) < 2:
        note += "; one run only: the interval is not informative"
    out.append(
        Metric(
            name=name,
            value=statistics.median(lags),
            ci_low=lo,
            ci_high=hi,
            n=len(done),
            method=method,
            note=note,
        )
    )
    return out


# =========================================================================== write


def write(summary: StudySummary, out_dir: Path | None = None) -> Path:
    """Save ``summary`` as ``<out_dir>/study.json`` (default ``runs/study``). Returns the path."""
    out_dir = Path(out_dir) if out_dir is not None else Path("runs") / STUDY_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "study.json"
    path.write_text(summary.model_dump_json(indent=2))
    return path
