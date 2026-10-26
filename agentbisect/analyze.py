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

# --------------------------------------------------------------------------- piped pytest (§4.2)

_WRAPPER_WORDS = frozenset(
    {"uv", "run", "poetry", "pipenv", "env", "time", "exec", "nohup", "sudo", "command", "coverage"}
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_NAME = re.compile(r"^python[\d.]*$")


def _split_command(command: str) -> list[tuple[str, str]]:
    """``(segment, separator-after)`` pairs. Separators: ``&&``, ``||``, ``;``, ``|``, newline.

    Quotes and backslashes are respected, so ``-k "a|b"`` does not split. The last segment's
    separator is ``""``. A lone ``&`` (as in ``2>&1``) is not a separator.
    """
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                buf.append(command[i])
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < len(command):
            buf.append(c + command[i + 1])
            i += 1
        else:
            two = command[i : i + 2]
            sep = two if two in ("&&", "||") else c if c in ";|\n" else ""
            if sep:
                parts.append(("".join(buf), sep))
                buf = []
                i += len(sep) - 1
            else:
                buf.append(c)
        i += 1
    parts.append(("".join(buf), ""))
    return parts


def _segment_runs_pytest(segment: str) -> bool:
    """True if the command segment calls pytest itself (not, say, ``grep pytest log``)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    for tok in tokens:
        tok = tok.lstrip("({")
        base = posixpath.basename(tok)
        if base in ("pytest", "py.test"):
            return True
        if tok == "-m" or tok.startswith("-") or _ENV_ASSIGN.match(tok):
            continue
        if base in _WRAPPER_WORDS or _PYTHON_NAME.match(base):
            continue
        return False
    return False


def runs_pytest(command: str) -> bool:
    """True if some part of ``command`` calls pytest (``pytest``, ``py.test``, ``-m pytest``)."""
    return any(_segment_runs_pytest(seg) for seg, _sep in _split_command(command))


def is_piped_pytest(command: str) -> bool:
    """True if the first pytest call in ``command`` pipes its output on with a lone ``|``.

    ``pytest -q | tail -5`` and ``pytest 2>&1 | tee log`` qualify: the shell reports the exit
    code of the last command in the pipe, so it says nothing about pytest. ``pytest || true``,
    ``cat old.log | head`` and ``grep FAILED log`` do not. A command that sets ``pipefail``
    reports the real exit code and is not treated as piped (METHODS §4.2).
    """
    if "pipefail" in command:
        return False
    for seg, sep in _split_command(command):
        if _segment_runs_pytest(seg):
            return sep == "|"
    return False


_PARAM_SUFFIX = re.compile(r"\[.*\]$")


def step_command(step: Step) -> str:
    """The shell command of a step. Recorders store it as ``args["command"]`` (or ``cmd``)."""
    args = step.args or {}
    return str(args.get("command") or args.get("cmd") or "")


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
        self._p1b: list[re.Pattern[str]] = []
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
                # P1b: the test's file failed to collect, so pytest prints no test id.
                self._p1b.append(re.compile(rf"^ERROR \S*(?<![\w.-]){re.escape(ref.file)}(?!::)\b"))
        else:
            # The check names no test (e.g. the whole suite): any failing test is the same test.
            self._p1.append(re.compile(r"^(?:FAILED|ERROR) \S"))
            self._p1.append(re.compile(r"^\S+::\S+ (?:FAILED|ERROR)\b"))  # pytest -v lines
            self._p2.append(re.compile(r"^_{3,} \S.* _{3,}$"))
            self._p3.append(re.compile(r"^_{3,} ERROR collecting \S+ _{3,}$"))
        self._p4 = re.compile(r'File "([^"]+)", line \d+')
        self._p5 = re.compile(r"^(\S+\.py):\d+: ")

    def _line_matches(self, line: str, *, test_specific_only: bool = False) -> bool:
        """True if one output line is a failure signal. Colors are removed first.

        ``test_specific_only`` leaves out the traceback patterns P4/P5 (piped pytest, exit 0).
        """
        stripped = strip_ansi(line).rstrip()
        if any(p.search(stripped) for p in self._p1):
            return True
        if any(p.match(stripped) for p in self._p1b):
            return True
        if any(p.match(stripped) for p in self._p2) or any(p.match(stripped) for p in self._p3):
            return True
        if self.touched and not test_specific_only:
            m4 = self._p4.search(stripped)
            if m4 and any(_ends_with(m4.group(1), t) for t in self.touched):
                return True
            m5 = self._p5.match(stripped)
            if m5 and any(_ends_with(m5.group(1), t) for t in self.touched):
                return True
        return False

    def signal(self, step: Step) -> str | None:
        """The first matching line of ``step``'s output, or None if the step shows no failure.

        Exit code 0 disqualifies a step unless it ran pytest into a pipe (the pipe hides pytest's
        exit code); then only the test-specific patterns count.
        """
        if step.kind is not ToolKind.SHELL:  # file reads and edits echo file text, not results
            return None
        specific_only = False
        if step.exit_code == 0:  # None (unknown) is allowed
            if not is_piped_pytest(step_command(step)):
                return None
            specific_only = True
        for line in strip_ansi(step.output).splitlines():
            if self._line_matches(line, test_specific_only=specific_only):
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
    # No chance to look (§4.4): the break is on the last step and no later output can show it.
    # A planted edit lands after its step's tool ran, so even a shell step's output cannot show it.
    last = trace.steps[t_err - 1]
    no_opportunity = t_err == trace.n and (
        last.kind is not ToolKind.SHELL
        or (trace.meta.sabotage_step is not None and t_err == trace.meta.sabotage_step)
    )
    if no_opportunity:
        t_obs, signal = None, None
    else:
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
        no_opportunity=no_opportunity,
    )
