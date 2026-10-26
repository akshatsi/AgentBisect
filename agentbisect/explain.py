"""AI explanation of a culprit report. Cosmetic only.

The verdict (decisive step, lag, wasted tokens) comes from running tests. This module asks an
LLM for a short plain-language reading of that verdict. The text may be wrong, it never
changes the verdict, and every place that shows it labels it "AI explanation".
"""

from __future__ import annotations

import re

from agentbisect.llm import LLMClient
from agentbisect.models import CulpritReport

LABEL = "AI explanation"
MAX_WORDS = 120
MAX_DIFF_CHARS = 6000
MAX_OUTPUT_CHARS = 1500
MAX_REASONING_CHARS = 1200

SYSTEM = (
    "You explain to a developer why a coding agent's edit broke a test. "
    "You are given facts that were measured by running the tests. Do not question them and do not "
    "guess at facts you were not given. Write plain prose in at most 100 words: what the step "
    "changed, why that breaks the test, and one sentence on the agent's reasoning if it helps. "
    "No headings, no bullet points, no code blocks."
)


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


def build_prompt(culprit: CulpritReport) -> str:
    """The user message sent to the model. Public so tests can check what goes in."""
    parts = [f"Test command: {culprit.check}"]
    if culprit.step is not None:
        s = culprit.step
        parts.append(f"Decisive step: {s.index}, tool {s.tool}, args {_tail(str(s.args), 400)}")
        if s.reasoning:
            parts.append(
                f"Agent's reasoning before the step:\n{_tail(s.reasoning, MAX_REASONING_CHARS)}"
            )
    if culprit.diff:
        diff = culprit.diff[:MAX_DIFF_CHARS]
        cut = " (diff cut)" if len(culprit.diff) > MAX_DIFF_CHARS else ""
        parts.append(f"Diff of the step{cut}:\n{diff}")
    probes = culprit.bisect.probes
    failing = [p for p in probes if p.verdict.value == "fail" and p.runs]
    if failing:
        last = failing[0]
        for p in failing:
            if p.step_index == culprit.bisect.decisive_step:
                last = p
        parts.append(
            "Oracle output at the decisive step (tail):\n"
            + _tail(last.runs[-1].output_tail, MAX_OUTPUT_CHARS)
        )
    a = culprit.analysis
    if a is not None:
        if a.censored:
            parts.append(
                f"The agent never saw the failure in its tool output ({a.steps_after_err} steps later)."
            )
        else:
            parts.append(
                f"The agent first saw the failure at step {a.t_obs}, a lag of {a.lag} steps."
            )
    return "\n\n".join(parts)


def truncate_words(text: str, limit: int = MAX_WORDS) -> str:
    """Cut ``text`` to at most ``limit`` words, keeping its line breaks out of the way."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    cut = " ".join(words[:limit])
    # End on a full sentence if one is in the last third of the cut.
    m = list(re.finditer(r"[.!?](?=\s|$)", cut))
    if m and m[-1].end() > len(cut) * 2 // 3:
        return cut[: m[-1].end()]
    return cut + "..."


def explain(culprit: CulpritReport, client: LLMClient) -> str:
    """Return an explanation of at most 120 words. Does not change ``culprit``."""
    response = client.chat(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_prompt(culprit)},
        ],
        max_tokens=300,
    )
    text = (response.content or "").strip()
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return truncate_words(text)
