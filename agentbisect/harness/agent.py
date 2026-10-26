"""The agent under test: a small tool-calling loop that records every step.

``record()`` copies the task's fixture repo into ``runs/<id>/workdir/``, takes the base snapshot
(step 0), then loops: ask the model, run one tool call, and after every mutating step
(write, edit, shell) snapshot the workdir. It writes ``meta.json`` and ``trace.jsonl`` after
every step, so a crash keeps the partial trace.

Stopping:

* ``finish`` tool call: ``meta.finished = True``.
* ``max_steps`` reached, or the model twice in a row answers with no tool call: stops with
  ``finished = False``.
* ``SpendCapExceeded``: the partial trace is saved, ``record.json`` gets
  ``stop_reason = "spend_cap"``, and the exception is re-raised with the run directory attached
  as ``exc.run_dir``. A study must stop when the cap is hit, so raising is the honest signal.

The harness also writes ``record.json`` (stop reason, sabotage log, native-tool flag), which is
outside the ``Trace`` contract in ``models.py`` and read by nobody but humans.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from agentbisect import snapshot
from agentbisect.harness.tools import (
    FALLBACK_PROTOCOL,
    TOOL_SCHEMAS,
    Toolbox,
    ToolResult,
    tool_kind,
)
from agentbisect.llm import (
    PROJECT_ROOT,
    ChatResponse,
    FakeLLM,
    LLMClient,
    SpendCapExceeded,
    ToolCall,
    extract_tool_calls,
)
from agentbisect.models import (
    MUTATING_KINDS,
    SabotageSpec,
    Step,
    TaskSpec,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)

log = logging.getLogger("agentbisect.harness")

FAKE_RUN_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "fake_run_t01.jsonl"

SYSTEM_PROMPT = """\
You are a coding agent working in a Python project. The working directory is the project root.
Use the tools to read files, edit files, and run commands. Make one tool call per turn, and say
briefly what you are about to do before each call. Run the tests to check your work. When the
task is done, call `finish`."""

NUDGE = (
    "You replied without a tool call. Reply with exactly one tool call. "
    "If the task is done, call `finish`."
)

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n.*?```", re.DOTALL)


def load_task(path: Path) -> TaskSpec:
    """Read a task YAML. A relative ``repo`` is resolved against the project root."""
    spec = TaskSpec.model_validate(yaml.safe_load(Path(path).read_text()))
    return spec.model_copy(update={"repo": resolve_repo(spec.repo)})


def resolve_repo(repo: Path) -> Path:
    repo = Path(repo)
    if repo.is_absolute():
        return repo
    for base in (PROJECT_ROOT, Path.cwd()):
        if (base / repo).is_dir():
            return (base / repo).resolve()
    raise FileNotFoundError(f"task repo '{repo}' not found under {PROJECT_ROOT} or the cwd")


def _new_run_id(task: TaskSpec, label: str | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label or "agent").strip("-")
    return f"{task.id}-{label}-{stamp}"


def _reasoning(response: ChatResponse) -> str | None:
    text = _FENCE.sub("", response.content or "").strip()
    return text or None


# --------------------------------------------------------------------------- sabotage


class Sabotage:
    """Applies one find/replace to the workdir at the first mutating step at or after ``at_step``."""

    def __init__(self, spec: SabotageSpec | None, workdir: Path) -> None:
        self.spec = spec
        self.workdir = workdir
        self.done = spec is None
        self.step: int | None = None
        self.note = "no sabotage" if spec is None else "pending"

    def maybe_apply(self, step_index: int, kind: ToolKind) -> bool:
        spec = self.spec
        if self.done or spec is None or step_index < spec.at_step or kind not in MUTATING_KINDS:
            return False
        self.done = True
        path = self.workdir / spec.file
        text = path.read_text() if path.is_file() else None
        if text is None or spec.find not in text:
            reason = "file missing" if text is None else "find text not present"
            self.note = f"not applied at step {step_index}: {reason} in {spec.file}"
            log.warning("sabotage %s", self.note)
            return False
        path.write_text(text.replace(spec.find, spec.replace))
        self.step = step_index
        self.note = f"applied at step {step_index} to {spec.file}"
        return True


# --------------------------------------------------------------------------- the loop


def _assistant_message(response: ChatResponse, call: ToolCall, native: bool) -> dict[str, Any]:
    if native:
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in response.tool_calls
            ],
        }
    return {"role": "assistant", "content": response.content or ""}


def _result_messages(
    response: ChatResponse, call: ToolCall, output: str, native: bool
) -> list[dict[str, Any]]:
    if not native:
        return [{"role": "user", "content": f"Tool result for {call.name}:\n{output}"}]
    msgs = [{"role": "tool", "tool_call_id": call.id, "content": output}]
    for extra in response.tool_calls:
        if extra.id != call.id:
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": extra.id,
                    "content": "not run: make one tool call per turn",
                }
            )
    return msgs


def record(
    task: TaskSpec,
    client: LLMClient,
    runs_dir: Path,
    model_alias: str | None = None,
    run_id: str | None = None,
    *,
    native_tools: bool = True,
    shell_timeout_s: float = 60.0,
) -> Path:
    """Run the agent on ``task`` and record it. Returns the run directory ``runs_dir/<id>``.

    ``native_tools=False`` sends no tool schemas and describes the JSON action protocol in the
    system prompt instead (for models whose ``tool_calls`` flag in models.yaml is false).
    """
    runs_dir = Path(runs_dir)
    run_id = run_id or _new_run_id(task, model_alias or client.model)
    run_dir = runs_dir / run_id
    if run_dir.exists():
        raise FileExistsError(f"run directory {run_dir} already exists")
    workdir = run_dir / "workdir"
    shutil.copytree(
        resolve_repo(task.repo),
        workdir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc"),
    )
    base = snapshot.init_shadow(run_dir, workdir)

    meta = TraceMeta(
        run_id=run_id,
        source=TraceSource.ALAN,
        workdir=workdir.resolve(),
        base_snapshot=base,
        task_id=task.id,
        model=model_alias or client.model,
    )
    trace = Trace(meta=meta)
    toolbox = Toolbox(workdir, shell_timeout_s=shell_timeout_s)
    sabotage = Sabotage(task.sabotage, workdir.resolve())
    system = SYSTEM_PROMPT if native_tools else SYSTEM_PROMPT + "\n\n" + FALLBACK_PROTOCOL
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task.prompt},
    ]
    tools = TOOL_SCHEMAS if native_tools else None
    stop_reason = "max_steps"
    carry_in = carry_out = 0  # tokens spent on replies that produced no step
    nudged = False

    def save() -> None:
        meta.cost_usd = client.usage_total.cost_usd
        meta.sabotage_step = sabotage.step
        trace.save(run_dir)
        (run_dir / "record.json").write_text(
            json.dumps(
                {
                    "stop_reason": stop_reason,
                    "sabotage": sabotage.note,
                    "native_tools": native_tools,
                    "model": client.model,
                    "tokens_in": client.usage_total.tokens_in,
                    "tokens_out": client.usage_total.tokens_out,
                },
                indent=2,
            )
        )

    try:
        while trace.n < task.max_steps:
            t0 = time.monotonic()
            response = client.chat(messages, tools)
            calls = extract_tool_calls(response)
            if not calls:
                carry_in += response.usage.tokens_in
                carry_out += response.usage.tokens_out
                if nudged:
                    stop_reason = "no_tool_call"
                    break
                nudged = True
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append({"role": "user", "content": NUDGE})
                continue
            nudged = False
            call = calls[0]
            native = bool(response.tool_calls)
            messages.append(_assistant_message(response, call, native))

            index = trace.n + 1
            kind = tool_kind(call.name)
            if call.parse_error:
                result = ToolResult(
                    f"error: could not parse arguments for {call.name}: {call.parse_error}",
                    ok=False,
                )
            else:
                result = toolbox.execute(call.name, call.arguments)

            snap = tree = None
            if kind in MUTATING_KINDS:
                sabotage.maybe_apply(index, kind)
                snap, tree = snapshot.take_snapshot(run_dir, workdir, f"step {index}: {call.name}")
            step = Step(
                index=index,
                tool=call.name,
                kind=kind,
                args=dict(call.arguments),
                output=result.output,
                exit_code=result.exit_code,
                reasoning=_reasoning(response),
                snapshot=snap,
                tree=tree,
                tokens_in=response.usage.tokens_in + carry_in,
                tokens_out=response.usage.tokens_out + carry_out,
                duration_s=round(time.monotonic() - t0, 3),
            )
            carry_in = carry_out = 0
            trace.steps.append(step)
            save()
            messages.extend(_result_messages(response, call, result.output, native))
            if result.finished:
                meta.finished = True
                stop_reason = "finished"
                break
    except SpendCapExceeded as exc:
        stop_reason = "spend_cap"
        save()
        exc.run_dir = run_dir  # type: ignore[attr-defined]
        raise
    except BaseException:
        stop_reason = "error"
        save()
        raise
    save()
    return run_dir


# --------------------------------------------------------------------------- fake run


def make_fake_run(runs_dir: Path, run_id: str = "fake-t01") -> Path:
    """Record the scripted run in ``tests/fixtures/fake_run_t01.jsonl`` on task t01 via FakeLLM.

    Costs nothing and touches no network. The run breaks ``test_add_duplicate_raises`` at step 8
    and first shows it in the agent's output at step 11 (see ``tests/fixtures/make_fake_run_t01.py``).
    """
    task = load_task(PROJECT_ROOT / "tasks" / "t01.yaml")
    client = FakeLLM.from_jsonl(FAKE_RUN_FIXTURE, model="fake")
    return record(task, client, Path(runs_dir), model_alias="fake", run_id=run_id)


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """``python -m agentbisect.harness.agent --task tasks/t01.yaml --model flash``.

    ``--fake`` replays the scripted t01 run instead (free). Without it, the run uses Alan AI
    credits: it needs ``.env`` (see ``.env.example``) and a priced model in ``tasks/models.yaml``.
    """
    import argparse

    from agentbisect.llm import create_client, find_model, load_models

    ap = argparse.ArgumentParser(prog="python -m agentbisect.harness.agent")
    ap.add_argument("--task", type=Path, default=PROJECT_ROOT / "tasks" / "t01.yaml")
    ap.add_argument("--model", help="alias from tasks/models.yaml, e.g. flash")
    ap.add_argument("--runs-dir", type=Path, default=PROJECT_ROOT / "runs")
    ap.add_argument("--run-id")
    ap.add_argument("--fake", action="store_true", help="replay the scripted fake run; no network")
    args = ap.parse_args(argv)

    if args.fake:
        run_dir = make_fake_run(args.runs_dir, args.run_id or "fake-t01")
        print(f"fake run recorded in {run_dir}")
        return 0

    client = create_client(args.model)
    entry = find_model(load_models(), args.model) if args.model else None
    native = entry is None or entry.tool_calls is not False
    task = load_task(args.task)
    run_dir = None
    try:
        run_dir = record(task, client, args.runs_dir, args.model, args.run_id, native_tools=native)
    except SpendCapExceeded as exc:
        run_dir = getattr(exc, "run_dir", None)
        print(f"spend cap reached: {exc}")
    u = client.usage_total
    print(
        f"run: {run_dir}\ntokens in: {u.tokens_in}  tokens out: {u.tokens_out}  "
        f"cost: ${u.cost_usd:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
