"""Alan Code recorder: turn an OpenCode session into an AgentBisect trace.

Alan Code is OpenCode running inside the Alan desktop app. We cannot call the model ourselves, so
we watch OpenCode work. A small JS plugin (``opencode_plugin.js``, rendered into
``<workdir>/.opencode/plugin/agentbisect.js``) runs inside OpenCode and calls this module:

* after every tool call:   ``python -m agentbisect.hooks.opencode step --run-dir <run>``
  (JSON payload on stdin: ``tool, sessionID, callID, args, title, output, metadata``). Appends one
  ``Step`` to ``trace.jsonl``, injects the sabotage if the task has one, and snapshots the
  workdir after steps that may change files.
* on ``session.idle``:      ``python -m agentbisect.hooks.opencode finalize --run-dir <run>``
  Fills in what the tool hooks cannot see (the agent's text before each call, tokens per step,
  model, cost) from ``events.jsonl``, which the plugin fills with OpenCode bus events.

Both commands are idempotent and take a file lock on ``<run>/.lock``.

ASSUMPTIONS (no live Alan run has been seen yet; checked against the strings in OpenCode 1.19.3):

* hook inputs: ``tool.execute.before(input{tool,sessionID,callID}, output{args})`` and
  ``tool.execute.after(input{tool,sessionID,callID,args}, output{title,output,metadata})``.
* bus events: ``message.updated`` (``properties.info``: id, sessionID, role, modelID, providerID,
  tokens, cost) and ``message.part.updated`` (``properties.part``: id, messageID, sessionID, type
  in text, reasoning, tool, step-start, step-finish; tool parts carry ``callID, tool, state``;
  ``step-finish`` parts carry ``tokens`` and ``cost``).
* the bash tool reports its exit code in ``metadata.exit`` (also accepted: ``exitCode``,
  ``exit_code``, ``code``). If it is missing the step's ``exit_code`` is None, which the
  observed-step rule (METHODS.md section 4) treats as "unknown, allowed".

TOKEN RULE (finalize). One model request is one ``step-start`` ... ``step-finish`` group of parts.
Its tokens go to the FIRST tool call of that group; other tool calls of the group get 0, so
summing steps never counts a request twice. ``tokens_in = input + cache.read + cache.write``,
``tokens_out = output + reasoning``. A request with no tool call (the final answer) belongs to no
step. If a message has no ``step-finish`` parts, the message's own ``tokens`` go to its first tool
call.

FALLBACK. If ``tool.execute.after`` never fires, ``trace.jsonl`` stays empty. Then ``finalize``
rebuilds the steps from completed tool parts in ``events.jsonl`` (input, output, metadata), and
takes ONE snapshot of the workdir's final state, which it attaches to the last mutating step.
Limits: no per-step snapshots (bisect can only compare the base with the end), and no sabotage
injection. ``finalize.json`` records which mode ran.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentbisect import snapshot, workspace
from agentbisect.harness.agent import Sabotage, resolve_repo
from agentbisect.harness.tools import clip
from agentbisect.llm import PROJECT_ROOT
from agentbisect.models import (
    MUTATING_KINDS,
    Step,
    TaskSpec,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)

PLUGIN_TEMPLATE = Path(__file__).with_name("opencode_plugin.js")
PLUGIN_RELPATH = Path(".opencode") / "plugin" / "agentbisect.js"
MAX_REASONING_CHARS = 4000
MAX_ARG_CHARS = 20_000

WRITE_TOOLS = {"write"}
EDIT_TOOLS = {"edit", "multiedit", "patch", "apply_patch"}
SHELL_TOOLS = {"bash"}
READ_TOOLS = {"read", "glob", "grep", "list", "ls"}
#: Tools that are neither file nor shell tools but cannot change the repo.
HARMLESS_OTHER = {
    "todowrite",
    "todoread",
    "webfetch",
    "websearch",
    "codesearch",
    "skill",
    "question",
    "lsp",
}


# --------------------------------------------------------------------------- classification


def classify(tool: str) -> ToolKind:
    name = tool.lower()
    if name in WRITE_TOOLS:
        return ToolKind.WRITE
    if name in EDIT_TOOLS:
        return ToolKind.EDIT
    if name in SHELL_TOOLS:
        return ToolKind.SHELL
    if name in READ_TOOLS:
        return ToolKind.READ
    return ToolKind.OTHER


def should_snapshot(tool: str, kind: ToolKind) -> bool:
    """Snapshot after mutating tools, and after any unknown tool (``task`` subagents, MCP tools)."""
    if kind in MUTATING_KINDS:
        return True
    return kind is ToolKind.OTHER and tool.lower() not in HARMLESS_OTHER


def exit_code_from(metadata: Any) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("exit", "exitCode", "exit_code", "code"):
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _cap_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        return {}
    return {
        str(k): v if not isinstance(v, str) or len(v) <= MAX_ARG_CHARS else v[:MAX_ARG_CHARS]
        for k, v in args.items()
    }


# --------------------------------------------------------------------------- locking, logging


@contextlib.contextmanager
def _locked(run_dir: Path) -> Iterator[None]:
    with (run_dir / ".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _log(run_dir: Path, message: str) -> None:
    try:
        with (run_dir / "hook.log").open("a") as f:
            f.write(f"{datetime.now(UTC).isoformat()} {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- prepare


def render_plugin(run_dir: Path, python: str | None = None) -> str:
    """The plugin source with absolute paths baked in (as JS string literals)."""
    text = PLUGIN_TEMPLATE.read_text()
    values = {
        "__AGENTBISECT_PYTHON__": python or sys.executable,
        "__AGENTBISECT_RUN_DIR__": str(Path(run_dir).resolve()),
        "__AGENTBISECT_REPO_ROOT__": str(PROJECT_ROOT),
    }
    for placeholder, value in values.items():
        text = text.replace(f'"{placeholder}"', json.dumps(value))
    return text


def agent_prompt(prompt: str, python: str | Path | None = None) -> str:
    """The task prompt with ``python`` pinned to this venv (or to ``python``).

    Alan Code's shell has no ``python`` and its ``python3`` lacks pytest, so an agent told to run
    ``python -m pytest`` could never see a test fail.
    """
    python = shlex.quote(str(python or sys.executable))
    return prompt.rstrip().replace("`python -m pytest", f"`{python} -m pytest") + "\n"


def _init_local_git(workdir: Path) -> None:
    """Give the workdir its own git repo with one commit.

    The workdir sits under the user's project, so without this an agent's ``git status`` or
    ``git commit`` would walk up and act on the user's real repo. Snapshots exclude ``.git``.
    """
    env = snapshot._env()
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["commit", "-q", "--no-verify", "-m", "task start"],
    ):
        subprocess.run(["git", *args], cwd=workdir, env=env, check=True, timeout=60)


def _default_run_id(prefix: str, model_alias: str | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", model_alias or "alancode").strip("-")
    return f"{prefix}-{label}-{stamp}"


def _new_run_dir(runs_dir: Path, run_id: str) -> Path:
    run_dir = (Path(runs_dir) / run_id).resolve()
    if run_dir.exists():
        raise FileExistsError(f"run directory {run_dir} already exists")
    return run_dir


def _install_plugin(run_dir: Path, workdir: Path) -> None:
    """The plugin calls back into AgentBisect, so it always uses this interpreter, never the
    project's venv (which has no ``agentbisect``)."""
    plugin = workdir / PLUGIN_RELPATH
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(render_plugin(run_dir, sys.executable))


def prepare_opencode_run(
    task: TaskSpec,
    runs_dir: Path,
    model_alias: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Set up ``runs/<id>/`` for a hand-started Alan Code session. Returns the run directory.

    Creates the workdir (a copy of the task repo), the shadow repo with the base snapshot,
    ``meta.json``, ``task.json``, ``PROMPT.txt`` and the plugin at
    ``workdir/.opencode/plugin/agentbisect.js``.
    """
    run_dir = _new_run_dir(runs_dir, run_id or _default_run_id(task.id, model_alias))
    run_id = run_dir.name
    workdir = run_dir / "workdir"
    shutil.copytree(
        resolve_repo(task.repo),
        workdir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc", ".opencode"),
    )
    _init_local_git(workdir)
    base = snapshot.init_shadow(run_dir, workdir)
    meta = TraceMeta(
        run_id=run_id,
        source=TraceSource.ALAN_CODE,
        workdir=workdir.resolve(),
        base_snapshot=base,
        task_id=task.id,
        model=model_alias,
        python=Path(sys.executable),
    )
    Trace(meta=meta).save(run_dir)
    (run_dir / "task.json").write_text(task.model_dump_json(indent=2))
    (run_dir / "PROMPT.txt").write_text(agent_prompt(task.prompt))
    _install_plugin(run_dir, workdir)
    return run_dir


def _repo_name(origin: str) -> str:
    local = Path(origin).expanduser()
    name = local.resolve().name if local.is_dir() else origin.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.git$", "", name)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"


def repo_prompt(prompt: str, python: Path | str, test_command: str) -> str:
    """The user's prompt, with ``python -m pytest`` pinned to the project's venv, plus one line
    telling the agent how to run the tests (Alan's shell has no ``python`` or pytest)."""
    quoted = shlex.quote(str(python))
    command = test_command.replace("python", quoted, 1)
    note = (
        f"Run the tests with `{command}`. "
        "The shell has no `python` or `pytest` on its path, so use that interpreter."
    )
    return f"{agent_prompt(prompt, python).rstrip()}\n\n{note}\n"


def prepare_repo_run(
    origin: str,
    prompt: str,
    runs_dir: Path,
    *,
    model_alias: str | None = None,
    run_id: str | None = None,
    ref: str | None = None,
    setup: bool = True,
) -> Path:
    """Set up ``runs/<id>/`` for a hand-started Alan Code session on the user's own project.

    ``origin`` is a local directory or a git URL (``https://`` or ``file://``). The project is
    copied or cloned into ``runs/<id>/workdir``; the original is never modified. With
    ``setup=True`` the project's dependencies go into ``runs/<id>/venv`` (see ``workspace.py``,
    which runs the repo's build code, so use trusted repos only); with ``setup=False`` the
    AgentBisect interpreter is used. Raises ``workspace.SetupError`` if the install fails (the run
    directory and its ``setup.log`` stay for inspection).
    """
    run_dir = _new_run_dir(runs_dir, run_id or _default_run_id(_repo_name(origin), model_alias))
    run_id = run_dir.name
    workdir = run_dir / "workdir"
    try:
        workspace.materialize(origin, workdir, ref=ref)
        _init_local_git(workdir)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    python = workspace.setup_env(workdir, run_dir / "venv") if setup else Path(sys.executable)
    base = snapshot.init_shadow(run_dir, workdir)
    meta = TraceMeta(
        run_id=run_id,
        source=TraceSource.ALAN_CODE,
        workdir=workdir.resolve(),
        base_snapshot=base,
        task_id=None,
        model=model_alias,
        repo_origin=origin,
        python=python,
    )
    Trace(meta=meta).save(run_dir)
    test_command = workspace.detect_test_command(workdir)
    (run_dir / "PROMPT.txt").write_text(repo_prompt(prompt, python, test_command))
    _install_plugin(run_dir, workdir)
    return run_dir


# --------------------------------------------------------------------------- step


def _load_sabotage(run_dir: Path, workdir: Path) -> Sabotage:
    task_file = run_dir / "task.json"
    spec = None
    if task_file.exists():
        spec = TaskSpec.model_validate_json(task_file.read_text()).sabotage
    sab = Sabotage(spec, workdir)
    state_file = run_dir / "sabotage.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        sab.done = bool(state.get("done"))
        sab.step = state.get("step")
        sab.note = state.get("note", sab.note)
    return sab


def _save_sabotage(run_dir: Path, sab: Sabotage) -> None:
    (run_dir / "sabotage.json").write_text(
        json.dumps({"done": sab.done, "step": sab.step, "note": sab.note})
    )


def _step_lines(run_dir: Path) -> list[str]:
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return []
    return [x for x in path.read_text().splitlines() if x.strip()]


def record_step(run_dir: Path, payload: Mapping[str, Any]) -> Step | None:
    """Append one step for a finished tool call. Returns the step, or None for a repeat."""
    run_dir = Path(run_dir)
    tool = str(payload.get("tool") or "")
    if not tool:
        raise ValueError("payload has no 'tool'")
    call_id = payload.get("callID")
    with _locked(run_dir):
        meta = TraceMeta.model_validate_json((run_dir / "meta.json").read_text())
        lines = _step_lines(run_dir)
        if call_id and any(
            f'"_call":"{call_id}"' in x or f'"_call": "{call_id}"' in x for x in lines
        ):
            return None
        index = len(lines) + 1
        kind = classify(tool)
        sab = _load_sabotage(run_dir, meta.workdir)
        snap = tree = None
        if should_snapshot(tool, kind):
            if sab.maybe_apply(index, kind):
                meta.sabotage_step = sab.step
                (run_dir / "meta.json").write_text(meta.model_dump_json(indent=2))
            snap, tree = snapshot.take_snapshot(run_dir, meta.workdir, f"step {index}: {tool}")
        _save_sabotage(run_dir, sab)
        args = _cap_args(payload.get("args"))
        if payload.get("sessionID"):
            args["_session"] = payload["sessionID"]
        if call_id:
            args["_call"] = call_id
        step = Step(
            index=index,
            tool=tool,
            kind=kind,
            args=args,
            output=clip(_text(payload.get("output"))),
            exit_code=exit_code_from(payload.get("metadata")) if kind is ToolKind.SHELL else None,
            snapshot=snap,
            tree=tree,
        )
        with (run_dir / "trace.jsonl").open("a") as f:
            f.write(step.model_dump_json() + "\n")
        return step


# --------------------------------------------------------------------------- events


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Events from ``events.jsonl`` as ``{"type", "properties"}``. Bad lines are skipped."""
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("event"), dict):
            obj = obj["event"]
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            out.append({"type": obj["type"], "properties": obj.get("properties") or {}})
    return out


class _Calls:
    """What events.jsonl says about each tool call: its text before it, its tokens, its state."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.message_order: list[str] = []
        self.parts: dict[str, dict[str, Any]] = {}
        self.part_order: dict[str, list[str]] = {}
        self.idle_after_last_message = False
        for e in events:
            props = e["properties"]
            if e["type"] == "message.updated":
                info = props.get("info") or {}
                mid = info.get("id")
                if mid:
                    if mid not in self.messages:
                        self.message_order.append(mid)
                    self.messages[mid] = info
                    self.idle_after_last_message = False
            elif e["type"] == "message.part.updated":
                part = props.get("part") or {}
                pid, mid = part.get("id"), part.get("messageID")
                if pid and mid:
                    if pid not in self.parts:
                        self.part_order.setdefault(mid, []).append(pid)
                        if mid not in self.messages:
                            self.message_order.append(mid)
                            self.messages[mid] = {"id": mid}
                    self.parts[pid] = part
                    self.idle_after_last_message = False
            elif e["type"] == "session.idle":
                self.idle_after_last_message = True
        # call id -> {"reasoning": str|None, "tokens_in": int, "tokens_out": int, "part": dict}
        self.calls: dict[str, dict[str, Any]] = {}
        self.call_order: list[str] = []
        for mid in self.message_order:
            self._walk_message(mid)

    @staticmethod
    def _tokens(tokens: Any) -> tuple[int, int]:
        if not isinstance(tokens, Mapping):
            return 0, 0
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
        tin = (
            int(tokens.get("input") or 0)
            + int(cache.get("read") or 0)
            + int(cache.get("write") or 0)
        )
        tout = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        return tin, tout

    def _walk_message(self, mid: str) -> None:
        info = self.messages.get(mid, {})
        if info.get("role") not in (None, "assistant"):
            return
        texts: list[str] = []
        group: list[str] = []  # call ids since the last step-start/step-finish
        saw_finish = False
        for pid in self.part_order.get(mid, []):
            part = self.parts[pid]
            ptype = part.get("type")
            if ptype == "step-start":
                texts, group = [], []
            elif ptype in ("text", "reasoning"):
                if part.get("text") and not part.get("synthetic"):
                    texts.append(str(part["text"]).strip())
            elif ptype == "tool":
                cid = part.get("callID") or part.get("id")
                reasoning = "\n\n".join(t for t in texts if t) or None
                if reasoning and len(reasoning) > MAX_REASONING_CHARS:
                    reasoning = reasoning[-MAX_REASONING_CHARS:]
                self.calls[cid] = {
                    "reasoning": reasoning,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "part": part,
                }
                self.call_order.append(cid)
                group.append(cid)
                texts = []
            elif ptype == "step-finish":
                saw_finish = True
                if group:
                    tin, tout = self._tokens(part.get("tokens"))
                    self.calls[group[0]]["tokens_in"] += tin
                    self.calls[group[0]]["tokens_out"] += tout
                texts, group = [], []
        if not saw_finish:
            mine = [c for c in self.call_order if self.calls[c]["part"].get("messageID") == mid]
            if mine:
                tin, tout = self._tokens(info.get("tokens"))
                self.calls[mine[0]]["tokens_in"] += tin
                self.calls[mine[0]]["tokens_out"] += tout

    def model(self) -> str | None:
        for mid in self.message_order:
            info = self.messages[mid]
            if info.get("role") == "assistant" and info.get("modelID"):
                return str(info["modelID"])
        return None

    def cost(self) -> float:
        total = 0.0
        for info in self.messages.values():
            if info.get("role") == "assistant":
                with contextlib.suppress(TypeError, ValueError):
                    total += float(info.get("cost") or 0)
        return total


def _step_from_part(index: int, part: Mapping[str, Any]) -> Step | None:
    state = part.get("state") or {}
    if state.get("status") not in ("completed", "error"):
        return None
    tool = str(part.get("tool") or "")
    kind = classify(tool)
    args = _cap_args(state.get("input"))
    if part.get("sessionID"):
        args["_session"] = part["sessionID"]
    args["_call"] = part.get("callID") or part.get("id")
    output = state.get("output") if state.get("status") == "completed" else state.get("error")
    return Step(
        index=index,
        tool=tool,
        kind=kind,
        args=args,
        output=clip(_text(output)),
        exit_code=exit_code_from(state.get("metadata")) if kind is ToolKind.SHELL else None,
    )


RESUME_MARKER = "<resume_context>"


def _resumed(run_dir: Path) -> bool:
    """True if Alan replayed an earlier conversation into this one.

    Alan prepends ``<resume_context>`` when a conversation is continued instead of started fresh.
    The agent then also works on the old task, so the run is not clean study data.
    """
    path = run_dir / "events.jsonl"
    return path.exists() and RESUME_MARKER in path.read_text(errors="replace")


def finalize_run(run_dir: Path) -> dict[str, Any]:
    """Merge ``events.jsonl`` into the trace. Safe to run any number of times."""
    run_dir = Path(run_dir)
    with _locked(run_dir):
        trace = Trace.load(run_dir)
        calls = _Calls(read_events(run_dir))
        mode = "hooks"
        if not trace.steps:
            mode = "events_only"
            for cid in calls.call_order:
                step = _step_from_part(trace.n + 1, calls.calls[cid]["part"])
                if step is not None:
                    trace.steps.append(step)
            last_mut = next((s for s in reversed(trace.steps) if s.kind in MUTATING_KINDS), None)
            if last_mut is not None:
                last_mut.snapshot, last_mut.tree = snapshot.take_snapshot(
                    run_dir, trace.meta.workdir, f"final state (events-only), step {last_mut.index}"
                )
        matched = 0
        for step in trace.steps:
            info = calls.calls.get(step.args.get("_call"))
            if info is None:
                continue
            matched += 1
            step.reasoning = info["reasoning"]
            step.tokens_in = info["tokens_in"]
            step.tokens_out = info["tokens_out"]
        model = calls.model()
        if model:
            trace.meta.model = trace.meta.model or model
        cost = calls.cost()
        if cost:
            trace.meta.cost_usd = cost
        trace.meta.finished = calls.idle_after_last_message and trace.n > 0
        trace.save(run_dir)
        summary = {
            "mode": mode,
            "steps": trace.n,
            "matched_with_events": matched,
            "model_seen": model,
            "finished": trace.meta.finished,
            "per_step_snapshots": mode == "hooks",
            "resumed_conversation": _resumed(run_dir),
        }
        (run_dir / "finalize.json").write_text(json.dumps(summary, indent=2))
        return summary


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """``step`` and ``finalize``. Exit 0 always, so the plugin never sees a failure; errors go to
    ``<run>/hook.log``."""
    ap = argparse.ArgumentParser(prog="python -m agentbisect.hooks.opencode")
    ap.add_argument("command", choices=["step", "finalize"])
    ap.add_argument("--run-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        if args.command == "step":
            payload = json.loads(sys.stdin.read())
            if not isinstance(payload, dict):
                raise ValueError("step payload is not a JSON object")
            record_step(args.run_dir, payload)
        else:
            finalize_run(args.run_dir)
    except BaseException:
        _log(args.run_dir, f"{args.command} failed:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
