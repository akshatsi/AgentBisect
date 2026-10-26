"""Plain-text helpers shared by the CLI verdict box and the HTML report."""

from __future__ import annotations

import contextlib
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentbisect.models import (
    Analysis,
    BisectStatus,
    CulpritReport,
    RepairEpisode,
    Step,
    TraceMeta,
)

#: How many test names a grouped verdict lists before it says "and N more".
MAX_LISTED = 5


def fmt_tokens(n: int) -> str:
    """41200 -> '41k', 1250000 -> '1.2M', 830 -> '830'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        k = n / 1000
        return f"{k:.0f}k" if k >= 10 else f"{k:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def fmt_percent(share: float) -> str:
    pct = share * 100
    return f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%"


def check_label(check: str) -> str:
    """Short name for what a check tests: the pytest test ID if there is one, else the command."""
    try:
        tokens = shlex.split(check)
    except ValueError:
        tokens = check.split()
    for token in tokens:
        if "::" in token:
            return token.rsplit("::", 1)[-1]
    for token in tokens:
        if token.endswith(".py"):
            return token
    return check


@dataclass(frozen=True)
class Shortener:
    """Turns absolute paths in a step label into short ones. Applies to labels, never to raw output.

    The run's venv python becomes ``python``, the workdir prefix drops away, and the run folder
    becomes ``<run>``.
    """

    python: tuple[str, ...] = ()
    workdirs: tuple[str, ...] = ()
    run_dirs: tuple[str, ...] = ()

    @classmethod
    def for_run(cls, meta: TraceMeta, run_dir: Path | None = None) -> Shortener:
        def variants(path: Path | None) -> list[str]:
            if path is None:
                return []
            out = [str(path)]
            with contextlib.suppress(OSError):
                out.append(str(path.resolve()))
            return out + [p.replace(" ", "\\ ") for p in out if " " in p]  # shell-escaped form

        runs = variants(run_dir) + variants(meta.workdir.parent)
        return cls(
            python=_longest_first(variants(meta.python)),
            workdirs=_longest_first(variants(meta.workdir)),
            run_dirs=_longest_first(runs),
        )

    def __call__(self, text: str) -> str:
        for path in self.python:
            text = re.sub(rf"(['\"]?){re.escape(path)}\1", "python", text)
        for path in self.workdirs:
            text = text.replace(path.rstrip("/") + "/", "")
            text = re.sub(rf"(?<![\w/.-]){re.escape(path)}(?![\w.-])", ".", text)
        for path in self.run_dirs:
            text = re.sub(rf"(?<![\w/.-]){re.escape(path)}(?![\w.-])", "<run>", text)
        return text


def _longest_first(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(p for p in paths if p), key=len, reverse=True))


_LABEL_ARGS = ("path", "file", "file_path", "command", "cmd")


def step_label(step: Step, shorten: Callable[[str], str] | None = None) -> str:
    """Short label such as ``edit calc/__init__.py`` or ``shell: pytest -q``.

    With ``shorten``, paths are shortened before the label is cut to length.
    """
    from agentbisect.bisect import describe_step

    if shorten is not None:
        args = {
            k: shorten(v) if k in _LABEL_ARGS and isinstance(v, str) else v
            for k, v in (step.args or {}).items()
        }
        step = step.model_copy(update={"args": args})
    return describe_step(step)


def _name_list(names: Sequence[str]) -> str:
    shown = list(names[:MAX_LISTED])
    extra = len(names) - len(shown)
    if extra > 0:
        shown.append(f"and {extra} more")
    return ", ".join(shown)


def analysis_sentences(a: Analysis, *, plural: bool = False) -> list[str]:
    """What the agent noticed, how late, and what that cost. Empty-safe for every case."""
    if a.censored or a.t_obs is None:
        if a.steps_after_err == 0:
            return [
                "This was the agent's last step, so it had no chance to see the failure.",
                f"The run ended with the {'tests' if plural else 'test'} broken.",
            ]
        return [
            "The agent never noticed.",
            f"It kept working for {a.steps_after_err} more "
            f"{'step' if a.steps_after_err == 1 else 'steps'} on a broken base "
            f"and spent {fmt_tokens(a.wasted_tokens)} tokens after the break "
            f"({fmt_percent(a.wasted_share)} of the run).",
        ]
    if a.lag == 0:
        return [f"The agent saw the failure at once, at step {a.t_obs}."]
    lag = a.lag if a.lag is not None else a.t_obs - a.t_err
    return [
        f"The agent only noticed at step {a.t_obs}, so it spent {lag} "
        f"{'step' if lag == 1 else 'steps'} working on a broken base.",
        f"It spent {fmt_tokens(a.wasted_tokens)} tokens after the break "
        f"({fmt_percent(a.wasted_share)} of the run).",
    ]


def analysis_key(a: Analysis | None) -> tuple | None:
    """Two culprits with the same key get the same lag and token sentences."""
    if a is None:
        return None
    return (
        a.t_err,
        a.t_obs,
        a.lag,
        a.censored or a.t_obs is None,
        a.steps_after_err,
        a.wasted_tokens,
        a.total_tokens,
    )


def shares_analysis(group: Sequence[CulpritReport]) -> bool:
    """True if every culprit in the group has the same lag and token numbers."""
    return len({analysis_key(c.analysis) for c in group}) <= 1


def group_by_step(culprits: Sequence[CulpritReport]) -> list[list[CulpritReport]]:
    """One group per decisive step, in order of first appearance.

    Culprits that were not bisected to a step stay alone.
    """
    groups: list[list[CulpritReport]] = []
    by_step: dict[int, list[CulpritReport]] = {}
    for c in culprits:
        t = c.bisect.decisive_step
        if c.bisect.status is BisectStatus.FOUND and t is not None:
            if t not in by_step:
                by_step[t] = []
                groups.append(by_step[t])
            by_step[t].append(c)
        else:
            groups.append([c])
    return groups


def group_verdict_sentences(
    group: Sequence[CulpritReport],
    step: Step | None,
    shorten: Callable[[str], str] | None = None,
) -> list[str]:
    """The verdict for culprits that share a decisive step. Empty if not bisectable."""
    if not group:
        return []
    t = group[0].bisect.decisive_step
    if t is None:
        return []
    labels = [check_label(c.check) for c in group]
    what = f"Step {t}"
    if step is not None:
        what += f" ({step_label(step, shorten)})"
    if len(group) == 1:
        out = [f"{what} broke {labels[0]}."]
    else:
        out = [f"{what} broke {len(group)} tests: {_name_list(labels)}."]
    if shares_analysis(group):
        a = group[0].analysis
        if a is not None:
            out.extend(analysis_sentences(a, plural=len(group) > 1))
        return out
    for c, label in zip(group[:MAX_LISTED], labels, strict=False):
        if c.analysis is not None:
            out.extend(f"{label}: {s}" for s in analysis_sentences(c.analysis))
    if len(group) > MAX_LISTED:
        out.append(f"The other {len(group) - MAX_LISTED} tests differ; see the per-test details.")
    return out


def verdict_sentences(
    culprit: CulpritReport,
    step: Step | None,
    shorten: Callable[[str], str] | None = None,
) -> list[str]:
    """The sentences of the verdict, in plain text. Empty if the run is not bisectable."""
    return group_verdict_sentences([culprit], step, shorten)


def _steps(n: int) -> str:
    return f"{n} {'step' if n == 1 else 'steps'}"


def repaired_sentences(
    episode: RepairEpisode, step: Step | None, shorten: Callable[[str], str] | None = None
) -> list[str]:
    """Plain-text sentences for a break the agent saw and fixed before the end of the run."""
    label = check_label(episode.check)
    if episode.t_err is None:
        out = [f"The agent saw {label} fail at step {episode.seen_at} and fixed it before the end."]
        reason = (episode.reason or "no reason recorded").rstrip(". ")
        out.append(f"The step that broke it was not found: {reason}.")
        return out
    what = f"Step {episode.t_err}"
    if step is not None:
        what += f" ({step_label(step, shorten)})"
    out = [f"{what} broke {label}."]
    if episode.seen_at == episode.t_err:
        out.append(
            f"The agent saw it at once, at step {episode.seen_at}, and fixed it before the end."
        )
    else:
        out.append(f"The agent saw it at step {episode.seen_at} and fixed it before the end.")
    lag = episode.lag if episode.lag is not None else episode.seen_at - episode.t_err
    if episode.wasted_tokens is not None and lag > 0:
        out.append(
            f"{_steps(lag)} and {fmt_tokens(episode.wasted_tokens)} tokens "
            "went into broken code first."
        )
    elif lag > 0:
        out.append(f"{_steps(lag)} went into broken code first.")
    return out
