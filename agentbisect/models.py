"""Data contracts shared by every module.

Anything that crosses a module boundary is defined here. Step indices are 1-based: step 0 is the
repo state before the agent did anything (``Trace.meta.base_snapshot``), and step ``i`` is the
state after the agent's ``i``-th tool call.

On disk a run lives in ``runs/<id>/``:

- ``meta.json``    – ``TraceMeta``
- ``trace.jsonl``  – one ``Step`` per line, in order
- ``shadow.git/``  – shadow repo, one commit per distinct snapshot
- ``scratch/``     – scratch worktree used to restore snapshots for the oracle
- ``bisect.json``  – ``RunReport`` written by ``agentbisect run``
- ``report.html``  – written by ``agentbisect report``
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- trace


class ToolKind(StrEnum):
    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    SHELL = "shell"
    FINISH = "finish"
    OTHER = "other"


#: Tool kinds that may change the repo and therefore get a snapshot.
MUTATING_KINDS = frozenset({ToolKind.WRITE, ToolKind.EDIT, ToolKind.SHELL})


class Step(BaseModel):
    """One agent tool call and the repo state after it."""

    index: int = Field(ge=1, description="1-based position in the trace.")
    tool: str = Field(description="Tool name as the agent called it, e.g. 'edit_file' or 'Bash'.")
    kind: ToolKind
    args: dict[str, Any] = Field(default_factory=dict)
    output: str = Field(
        default="", description="Tool output shown to the agent (may be truncated)."
    )
    exit_code: int | None = Field(default=None, description="Exit code for shell steps.")
    reasoning: str | None = Field(
        default=None, description="Agent text immediately before the call."
    )
    snapshot: str | None = Field(
        default=None,
        description="Shadow-git commit SHA of the repo after this step. None for read-only steps; "
        "consumers use the most recent earlier snapshot (or the base) for those.",
    )
    tree: str | None = Field(default=None, description="Git tree hash of the snapshot (dedup key).")
    tokens_in: int = 0
    tokens_out: int = 0
    duration_s: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class TraceSource(StrEnum):
    ALAN = "alan"
    CLAUDE_CODE = "claude_code"
    ALAN_CODE = "alan_code"  # OpenCode inside the Alan app, recorded by a plugin
    SYNTHETIC = "synthetic"


class TraceMeta(BaseModel):
    run_id: str
    source: TraceSource
    workdir: Path = Field(description="Directory the agent worked in (never written by bisect).")
    base_snapshot: str = Field(description="Shadow-git SHA of step 0, before any agent action.")
    task_id: str | None = None
    model: str | None = Field(default=None, description="Model alias from tasks/models.yaml.")
    sabotage_step: int | None = Field(
        default=None, description="Ground truth: the step index where a break was injected."
    )
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished: bool = Field(default=False, description="True if the agent called finish.")
    repo_origin: str | None = Field(
        default=None, description="Path or git URL the workdir was copied or cloned from."
    )
    python: Path | None = Field(
        default=None,
        description="Interpreter of the project's own venv. The oracle runs tests with it; "
        "None means the AgentBisect interpreter (fine for the practice repo).",
    )


class Trace(BaseModel):
    meta: TraceMeta
    steps: list[Step] = Field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.steps)

    def snapshot_at(self, index: int) -> str:
        """Shadow SHA of the repo state after step ``index`` (0 = base)."""
        for step in reversed(self.steps[:index]):
            if step.snapshot is not None:
                return step.snapshot
        return self.meta.base_snapshot

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.steps)

    # -- disk format -------------------------------------------------------

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(self.meta.model_dump_json(indent=2))
        with (run_dir / "trace.jsonl").open("w") as f:
            for step in self.steps:
                f.write(step.model_dump_json() + "\n")

    @classmethod
    def load(cls, run_dir: Path) -> Trace:
        meta = TraceMeta.model_validate_json((run_dir / "meta.json").read_text())
        lines = (run_dir / "trace.jsonl").read_text().splitlines()
        steps = [Step.model_validate_json(line) for line in lines if line.strip()]
        return cls(meta=meta, steps=steps)


# --------------------------------------------------------------------------- oracle


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # oracle could not run: timeout, collection error, missing test, etc.


class OracleConfig(BaseModel):
    check: str = Field(
        description="Shell command, e.g. 'pytest tests/test_auth.py::test_login -q'."
    )
    timeout_s: float = 120.0
    max_repeats: int = Field(
        default=1, ge=1, description="Upper bound on runs per snapshot. Policy is in METHODS.md §2."
    )
    flake_rate: float = Field(
        default=0.0, ge=0.0, lt=0.5, description="Assumed per-run flip probability (METHODS.md §2)."
    )


class OraclePolicy(BaseModel):
    """How many times to run the oracle per snapshot and when to stop.

    Built by ``stats.make_policy`` from the rules in METHODS.md §2; applied by ``oracle.py``
    through ``stats.decide``. ``oracle.py`` never hard-codes these numbers.
    """

    flake_rate: float = Field(ge=0.0, lt=0.5, description="Per-run flip probability assumed.")
    max_runs: int = Field(ge=1, description="Hard cap on runs per snapshot.")
    per_probe_error: float = Field(ge=0.0, le=1.0, description="Max error per oracle decision.")
    rule: str = Field(description="Human-readable name of the decision rule, for the bisect log.")


class OracleRun(BaseModel):
    """One execution of the check command."""

    exit_code: int | None
    duration_s: float
    output_tail: str = ""
    timed_out: bool = False


class OracleResult(BaseModel):
    """The oracle's decision for one snapshot, possibly over repeated runs."""

    step_index: int = Field(ge=0)
    snapshot: str
    verdict: Verdict
    runs: list[OracleRun] = Field(default_factory=list)

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    @property
    def n_fail(self) -> int:
        return sum(1 for r in self.runs if r.exit_code not in (0, None))


# --------------------------------------------------------------------------- bisect


class BisectStatus(StrEnum):
    FOUND = "found"
    NOT_BISECTABLE = "not_bisectable"


class BisectResult(BaseModel):
    check: str
    status: BisectStatus
    reason: str | None = Field(default=None, description="Why not bisectable, if so.")
    decisive_step: int | None = Field(default=None, ge=1, description="t_err.")
    probes: list[OracleResult] = Field(
        default_factory=list, description="Every oracle decision, in the order made."
    )
    precondition_probes: int = Field(default=0, description="Probes spent on step 0 and step n.")
    policy: OraclePolicy | None = Field(default=None, description="Oracle policy used.")
    non_monotone: bool = False
    transitions: list[int] = Field(
        default_factory=list,
        description="Every pass->fail step found, if the history is non-monotone (METHODS.md §3).",
    )
    confidence: float | None = Field(
        default=None, description="Probability the decisive step is correct (METHODS.md §2)."
    )

    @property
    def n_probes(self) -> int:
        return len(self.probes)


# --------------------------------------------------------------------------- analysis


class Analysis(BaseModel):
    """Detection lag for one decisive step (METHODS.md §4)."""

    t_err: int = Field(ge=1)
    t_obs: int | None = Field(default=None, description="None if censored.")
    lag: int | None = Field(default=None, description="t_obs - t_err; None if censored.")
    censored: bool
    steps_after_err: int = Field(description="n - t_err: exposure time for censored runs.")
    wasted_tokens: int = Field(description="Tokens spent in steps after t_err.")
    total_tokens: int
    observed_signal: str | None = Field(default=None, description="The line that matched at t_obs.")

    @property
    def wasted_share(self) -> float:
        return self.wasted_tokens / self.total_tokens if self.total_tokens else 0.0


class CulpritReport(BaseModel):
    run_id: str
    check: str
    bisect: BisectResult
    analysis: Analysis | None = None
    diff: str = Field(
        default="", description="Diff of the decisive step's snapshot vs the one before."
    )
    step: Step | None = Field(default=None, description="The decisive step itself.")
    explanation: str | None = Field(
        default=None,
        description="LLM text. Always shown labeled 'AI explanation'. Never a verdict.",
    )


class RunReport(BaseModel):
    """Everything `agentbisect run` found for one trace. Written to runs/<id>/bisect.json."""

    run_id: str
    checks: list[str] = Field(description="Oracle commands bisected (auto-oracle may yield many).")
    culprits: list[CulpritReport] = Field(default_factory=list)
    auto_oracle: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------- tasks and study


class TaskSpec(BaseModel):
    """A task YAML in tasks/."""

    id: str
    title: str
    prompt: str = Field(description="Instruction given to the agent.")
    repo: Path = Field(description="Fixture repo copied into a fresh workdir per run.")
    check: str | None = Field(default=None, description="Oracle; None means auto-oracle.")
    max_steps: int = 30
    sabotage: SabotageSpec | None = None


class SabotageSpec(BaseModel):
    """Inject a known break at a known step, for measuring bisect accuracy."""

    at_step: int = Field(ge=1)
    file: str
    find: str
    replace: str


class LLMConfig(BaseModel):
    base_url: str
    api_key: str = Field(repr=False)
    model: str
    spend_cap_usd: float = 8.00
    timeout_s: float = 120.0


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class StudyRow(BaseModel):
    """One agent run in the study. One row per (run, culprit); runs without a break have one row."""

    run_id: str
    task_id: str
    model: str
    sabotage_step: int | None = None
    n_steps: int
    final_failed: bool = Field(description="Oracle fails at the final step.")
    bisectable: bool
    check: str | None = None
    t_err: int | None = None
    t_obs: int | None = None
    lag: int | None = None
    censored: bool | None = None
    steps_after_err: int | None = None
    wasted_tokens: int | None = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    sabotage_correct: bool | None = Field(default=None, description="t_err == sabotage_step.")


TaskSpec.model_rebuild()


class RepairEpisode(BaseModel):
    """A break the agent saw and later fixed: bisected over steps 0..k (METHODS §8)."""

    run_id: str
    check: str
    seen_at: int = Field(ge=1, description="Agent pytest step k whose output showed the failure.")
    t_err: int | None = Field(default=None, description="Decisive step in 0..k; None if unfound.")
    lag: int | None = None
    wasted_tokens: int | None = None
    reason: str | None = Field(default=None, description="Why it could not be bisected, if so.")


class Metric(BaseModel):
    """One reported number with its interval, for RESULTS.md and the study page."""

    name: str
    value: float | None = Field(description="None when not estimable (e.g. KM median not reached).")
    ci_low: float | None = None
    ci_high: float | None = None
    n: int = Field(description="Sample size behind the number.")
    method: str = Field(description="E.g. 'Wilson 95%', 'Kaplan-Meier, Greenwood log-log'.")
    note: str | None = None


class StudySummary(BaseModel):
    """Everything `agentbisect study` computes. Written to runs/study/study.json."""

    rows: list[StudyRow] = Field(default_factory=list)
    excluded: dict[str, str] = Field(
        default_factory=dict, description="run_id -> reason it is not study data."
    )
    episodes: list[RepairEpisode] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    design: str = Field(default="", description="Which METHODS §8 variant, in one line.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
