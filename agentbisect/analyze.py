"""Detection lag: when did the agent first see the failure that a step caused?

Implements docs/METHODS.md §4. Given the decisive step ``t_err`` (from bisect), find the observed
step ``t_obs``: the first step at or after ``t_err`` whose shell output shows a failure signal for
the same test. If none does, the lag is censored. Everything here is deterministic text matching;
no LLM is involved.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agentbisect import snapshot
from agentbisect.models import Analysis, BisectResult, BisectStatus, Step, ToolKind, Trace

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove terminal color codes. Projects that set ``--color=yes`` wrap every result line."""
    return _ANSI.sub("", text)


SIGNAL_MAX_CHARS = 200

_PARAM_SUFFIX = re.compile(r"\[.*\]$")


# --------------------------------------------------------------------------- test keys (§4.1)


@dataclass(frozen=True)
class TestRef:
    """One test (or test file) named by the check command."""

    __test__ = False  # not a pytest test class

    file: str  # basename, e.g. "test_auth.py"
    key: str  # "test_auth.py::TestC::test_f" or just the basename
    function: str | None = None  # "test_f"
    qualified: str | None = None  # "TestC.test_f", or "test_f" without a class


def parse_check(check: str) -> list[TestRef]:
    """Test references in a check command (§4.1).

    Every argument with ``::`` is a node ID (a ``[...]`` parameter suffix is dropped). If the
    command runs pytest and names only files, each file is a reference. A command that names
    neither (e.g. the whole suite) gives an empty list.
    """
    try:
        tokens = shlex.split(check)
    except ValueError:
        tokens = check.split()
    refs: list[TestRef] = []
    is_pytest = "pytest" in check or "py.test" in check
    for token in tokens:
        if token.startswith("-"):
            continue
        if "::" in token:
            parts = _PARAM_SUFFIX.sub("", token).split("::")
            base = posixpath.basename(parts[0].replace("\\", "/"))
            if not base or len(parts) < 2:
                continue
            refs.append(
                TestRef(
                    file=base,
                    key="::".join([base, *parts[1:]]),
                    function=parts[-1],
                    qualified=".".join(parts[1:]),
                )
            )
        elif is_pytest and token.endswith(".py"):
            base = posixpath.basename(token.replace("\\", "/"))
            refs.append(TestRef(file=base, key=base))
    return refs


# --------------------------------------------------------------------------- signal (§4.2)


def _path_parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p and p != "."]


def _ends_with(path: str, suffix_parts: list[str]) -> bool:
    """True if ``path`` ends with ``suffix_parts`` on whole path components."""
    parts = _path_parts(path)
    return len(parts) >= len(suffix_parts) and parts[-len(suffix_parts) :] == suffix_parts


class SignalMatcher:
    """Finds a failure signal for one check in a block of tool output."""

    def __init__(self, check: str, touched_files: Iterable[str]) -> None:
        self.refs = parse_check(check)
        self.touched = [_path_parts(f) for f in touched_files if f.endswith(".py")]
        status = r"\b(?:FAILED|ERROR)\b"
        self._p1: list[re.Pattern[str]] = []
        self._p2: list[re.Pattern[str]] = []
        self._p3: list[re.Pattern[str]] = []
        if self.refs:
            for ref in self.refs:
                # The key must stand alone: "test_f" must not match "test_f2".
                key = r"(?<![\w.-])" + re.escape(ref.key) + r"(?!\w)"
                self._p1.append(re.compile(status + r".*" + key + r"|" + key + r".*" + status))
                if ref.qualified is not None:
                    name = re.escape(ref.qualified)
                    self._p2.append(re.compile(rf"^_{{3,}} {name}(?:\[.*\])? _{{3,}}$"))
                self._p3.append(
                    re.compile(rf"^_{{3,}} ERROR collecting \S*{re.escape(ref.file)} _{{3,}}$")
                )
        else:
            # The check names no test (e.g. the whole suite): any failing test is the same test.
            self._p1.append(re.compile(r"^(?:FAILED|ERROR) \S"))
            self._p1.append(re.compile(r"^\S+::\S+ (?:FAILED|ERROR)\b"))  # pytest -v lines
            self._p2.append(re.compile(r"^_{3,} \S.* _{3,}$"))
            self._p3.append(re.compile(r"^_{3,} ERROR collecting \S+ _{3,}$"))
        self._p4 = re.compile(r'File "([^"]+)", line \d+')
        self._p5 = re.compile(r"^(\S+\.py):\d+: ")

    def _line_matches(self, line: str) -> bool:
        stripped = line.rstrip()
        if any(p.search(stripped) for p in self._p1):
            return True
        if any(p.match(stripped) for p in self._p2) or any(p.match(stripped) for p in self._p3):
            return True
        if self.touched:
            m4 = self._p4.search(stripped)
            if m4 and any(_ends_with(m4.group(1), t) for t in self.touched):
                return True
            m5 = self._p5.match(stripped)
            if m5 and any(_ends_with(m5.group(1), t) for t in self.touched):
                return True
        return False

    def signal(self, step: Step) -> str | None:
        """The first matching line of ``step``'s output, or None if the step shows no failure."""
        if step.kind is not ToolKind.SHELL:  # file reads and edits echo file text, not results
            return None
        if step.exit_code == 0:  # None (unknown) is allowed
            return None
        for line in strip_ansi(step.output).splitlines():
            if self._line_matches(line):
                return line.strip()[:SIGNAL_MAX_CHARS]
        return None


def find_observation(
    steps: Iterable[Step], t_err: int, check: str, touched_files: Iterable[str]
) -> tuple[int | None, str | None]:
    """``(t_obs, observed_signal)`` per §4.3, or ``(None, None)`` if the agent never noticed."""
    matcher = SignalMatcher(check, touched_files)
    for step in steps:
        if step.index < t_err:
            continue
        line = matcher.signal(step)
        if line is not None:
            return step.index, line
    return None, None


# --------------------------------------------------------------------------- analysis (§4.3)


def touched_files(trace: Trace, run_dir: Path, t_err: int) -> list[str]:
    """Python files the decisive step changed (snapshot before it vs after it)."""
    changed = snapshot.changed_files(
        run_dir, trace.snapshot_at(t_err - 1), trace.snapshot_at(t_err)
    )
    return [f for f in changed if f.endswith(".py")]


def analyze(trace: Trace, bisect: BisectResult, run_dir: Path) -> Analysis:
    """Detection lag for the decisive step in ``bisect`` (METHODS.md §4).

    For a non-monotone history ``bisect.decisive_step`` is already the last transition (§3.1).
    """
    if bisect.status is not BisectStatus.FOUND or bisect.decisive_step is None:
        raise ValueError("analyze needs a FOUND bisect result")
    t_err = bisect.decisive_step
    if not 1 <= t_err <= trace.n:
        raise ValueError(f"decisive step {t_err} is outside the trace (1..{trace.n})")
    touched = touched_files(trace, Path(run_dir).resolve(), t_err)
    t_obs, signal = find_observation(trace.steps, t_err, bisect.check, touched)
    return Analysis(
        t_err=t_err,
        t_obs=t_obs,
        lag=None if t_obs is None else t_obs - t_err,
        censored=t_obs is None,
        steps_after_err=trace.n - t_err,
        wasted_tokens=sum(s.tokens for s in trace.steps if s.index > t_err),
        total_tokens=sum(s.tokens for s in trace.steps),
        observed_signal=signal,
    )
