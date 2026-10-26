"""Plain-text helpers shared by the CLI verdict box and the HTML report."""

from __future__ import annotations

import shlex

from agentbisect.models import CulpritReport, Step


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


def step_label(step: Step) -> str:
    """Short label such as ``edit calc/__init__.py`` or ``shell: pytest -q``."""
    from agentbisect.bisect import describe_step

    return describe_step(step)


def verdict_sentences(culprit: CulpritReport, step: Step | None) -> list[str]:
    """The sentences of the verdict, in plain text. Empty if the run is not bisectable."""
    b = culprit.bisect
    if b.decisive_step is None:
        return []
    label = check_label(culprit.check)
    what = f"Step {b.decisive_step}"
    if step is not None:
        what += f" ({step_label(step)})"
    out = [f"{what} broke {label}."]
    a = culprit.analysis
    if a is None:
        return out
    if a.censored or a.t_obs is None:
        out.append("The agent never noticed.")
        out.append(
            f"It kept working for {a.steps_after_err} more "
            f"{'step' if a.steps_after_err == 1 else 'steps'} on a broken base "
            f"and spent {fmt_tokens(a.wasted_tokens)} tokens after the break "
            f"({fmt_percent(a.wasted_share)} of the run)."
        )
    elif a.lag == 0:
        out.append(f"The agent saw the failure at once, at step {a.t_obs}.")
    else:
        lag = a.lag if a.lag is not None else a.t_obs - a.t_err
        out.append(
            f"The agent only noticed at step {a.t_obs}, so it spent {lag} "
            f"{'step' if lag == 1 else 'steps'} working on a broken base."
        )
        out.append(
            f"It spent {fmt_tokens(a.wasted_tokens)} tokens after the break "
            f"({fmt_percent(a.wasted_share)} of the run)."
        )
    return out
