"""Claude Code hook recorder: turn a live Claude Code session into an AgentBisect trace.

Claude Code runs this module as a hook command and sends one JSON event on stdin::

    python -m agentbisect.hooks.claude_code

Events handled (dispatch on ``hook_event_name``):

* ``SessionStart``       take the base snapshot (step 0) before the agent does anything.
* ``PostToolUse``        a tool call finished. Record a step, and snapshot if it may change files.
* ``PostToolUseFailure`` a tool call failed. A Bash command that exits non-zero lands here
  (first line of ``error`` is ``Exit code N``), and so does a failing pytest run, so it must be
  registered or the recorder never sees the agent's failure signals.
* ``SessionEnd``         optional. Marks the trace finished.

The run lands in ``<runs>/cc-<session_id>/`` in the same format as the Alan harness
(``meta.json``, ``trace.jsonl``, ``shadow.git``), with ``source = claude_code``. The user's own
directory is only read: snapshots go to the shadow repo. Tokens and the agent's reasoning come
from the session transcript when it can be read (best effort); otherwise they are 0 and None.

This hook must never block or fail the session. It prints nothing, exits 0 on every error, and
writes errors to ``<runs>/cc-<session_id>/hook.log`` (or ``<runs>/hook-errors.log``).

Runs directory: ``$AGENTBISECT_RUNS_DIR``, else ``runs/`` in the AgentBisect checkout.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import traceback
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentbisect import snapshot
from agentbisect.harness.tools import clip
from agentbisect.llm import PROJECT_ROOT
from agentbisect.models import MUTATING_KINDS, Step, ToolKind, TraceMeta, TraceSource

TRANSCRIPT_TAIL_BYTES = 4_000_000
MAX_ARG_CHARS = 20_000

WRITE_TOOLS = {"Write"}
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
#: Tools that are not file or shell tools but can still change the repo (subagents, MCP servers).
SNAPSHOT_OTHER_PREFIXES = ("Task", "Agent", "mcp__")

_EXIT_RE = re.compile(r"^Exit code (-?\d+)")


def runs_root() -> Path:
    env = os.environ.get("AGENTBISECT_RUNS_DIR")
    return Path(env) if env else PROJECT_ROOT / "runs"


def classify(tool_name: str) -> ToolKind:
    if tool_name in WRITE_TOOLS:
        return ToolKind.WRITE
    if tool_name in EDIT_TOOLS:
        return ToolKind.EDIT
    if tool_name in SHELL_TOOLS:
        return ToolKind.SHELL
    if tool_name in READ_TOOLS:
        return ToolKind.READ
    return ToolKind.OTHER


def should_snapshot(tool_name: str, kind: ToolKind) -> bool:
    return kind in MUTATING_KINDS or tool_name.startswith(SNAPSHOT_OTHER_PREFIXES)


def run_id_for(session_id: str) -> str:
    return "cc-" + re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-")[:80]


# --------------------------------------------------------------------------- event parsing


def _cap_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        out[k] = v if not isinstance(v, str) or len(v) <= MAX_ARG_CHARS else v[:MAX_ARG_CHARS]
    return out


def _stringify(response: Any, kind: ToolKind, tool_name: str) -> str:
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if tool_name in SHELL_TOOLS:
            return "".join(
                response.get(k) or ""
                for k in ("stdout", "stderr")
                if isinstance(response.get(k), str)
            )
        if kind in (ToolKind.EDIT, ToolKind.WRITE):
            path = response.get("filePath") or response.get("file_path") or ""
            verb = response.get("type") or "edited"
            return f"{verb} {path}".strip()
        file_info = response.get("file")
        if isinstance(file_info, dict) and isinstance(file_info.get("content"), str):
            return file_info["content"]
    try:
        return json.dumps(response, default=str)
    except (TypeError, ValueError):
        return str(response)


def parse_event(event: Mapping[str, Any]) -> tuple[str, int | None]:
    """Return ``(output, exit_code)`` for a PostToolUse or PostToolUseFailure event."""
    name = str(event.get("tool_name", ""))
    kind = classify(name)
    if event.get("hook_event_name") == "PostToolUseFailure" or "error" in event:
        error = str(event.get("error") or "")
        m = _EXIT_RE.match(error)
        if m:
            body = error.split("\n", 1)[1] if "\n" in error else ""
            return clip(body), int(m.group(1))
        return clip(error), None
    output = _stringify(event.get("tool_response", event.get("tool_result")), kind, name)
    response = event.get("tool_response")
    interrupted = isinstance(response, dict) and bool(response.get("interrupted"))
    exit_code = 0 if name in SHELL_TOOLS and not interrupted else None
    return clip(output), exit_code


# --------------------------------------------------------------------------- transcript


def _read_tail_lines(path: Path) -> list[dict[str, Any]]:
    size = path.stat().st_size
    with path.open("rb") as f:
        start = max(0, size - TRANSCRIPT_TAIL_BYTES)
        f.seek(start)
        data = f.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # first line is probably cut
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def from_transcript(
    transcript_path: str | None, tool_use_id: str | None
) -> tuple[str | None, int, int, str | None]:
    """Best effort: ``(reasoning, tokens_in, tokens_out, model)`` for the model turn that made the call.

    One model response can hold several tool calls; Claude Code writes one transcript line per
    content block with the same ``message.id`` and usage. Tokens go to the first tool call of the
    response so that summing steps does not count a response twice.
    """
    if not transcript_path or not tool_use_id:
        return None, 0, 0, None
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return None, 0, 0, None
        entries = _read_tail_lines(path)
    except OSError:
        return None, 0, 0, None
    msg_id = None
    for e in entries:
        msg = e.get("message")
        if e.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id
            for b in content
        ):
            msg_id = msg.get("id")
            break
    if msg_id is None:
        return None, 0, 0, None
    texts: list[str] = []
    tool_ids: list[str] = []
    usage: dict[str, Any] = {}
    model = None
    for e in entries:
        msg = e.get("message")
        if e.get("type") != "assistant" or not isinstance(msg, dict) or msg.get("id") != msg_id:
            continue
        model = msg.get("model") or model
        # Lines of one response repeat its usage, early lines sometimes with partial counts.
        for key, value in (msg.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = max(int(usage.get(key) or 0), value)
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                if not tool_ids:  # only text that comes before the first call
                    texts.append(b["text"].strip())
            elif b.get("type") == "tool_use":
                tool_ids.append(b.get("id"))
    reasoning = "\n\n".join(texts) or None
    if tool_ids and tool_ids[0] != tool_use_id:
        return None, 0, 0, model
    tin = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    tout = int(usage.get("output_tokens") or 0)
    return reasoning, tin, tout, model


# --------------------------------------------------------------------------- recording


@contextlib.contextmanager
def _locked(run_dir: Path) -> Iterator[None]:
    """Serialize hooks of one session. Claude Code may run hooks for parallel tool calls at once."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _log(run_dir: Path, message: str) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "hook.log").open("a") as f:
            f.write(f"{datetime.now(UTC).isoformat()} {message}\n")
    except OSError:
        pass


def _meta_path(run_dir: Path) -> Path:
    return run_dir / "meta.json"


def _ensure_run(run_dir: Path, session_id: str, workdir: Path, note_late: bool) -> TraceMeta:
    """Load the run's meta, creating the run (and its base snapshot) on first sight."""
    if _meta_path(run_dir).exists():
        return TraceMeta.model_validate_json(_meta_path(run_dir).read_text())
    base = snapshot.init_shadow(run_dir, workdir)
    meta = TraceMeta(
        run_id=run_dir.name,
        source=TraceSource.CLAUDE_CODE,
        workdir=workdir,
        base_snapshot=base,
    )
    _meta_path(run_dir).write_text(meta.model_dump_json(indent=2))
    (run_dir / "trace.jsonl").touch()
    if note_late:
        _log(
            run_dir,
            "base snapshot taken after the first tool call (no SessionStart hook): "
            "step 1 may already be part of the base",
        )
    return meta


def _step_count(run_dir: Path) -> int:
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _seen(run_dir: Path, tool_use_id: str | None, add: bool) -> bool:
    if not tool_use_id:
        return False
    path = run_dir / "hook_seen.txt"
    seen = set(path.read_text().split()) if path.exists() else set()
    if tool_use_id in seen:
        return True
    if add:
        with path.open("a") as f:
            f.write(tool_use_id + "\n")
    return False


def handle_event(event: Mapping[str, Any], runs_dir: Path | None = None) -> Path | None:
    """Record one hook event. Returns the run directory, or None if the event was ignored."""
    session_id = str(event.get("session_id") or "").strip()
    if not session_id:
        return None
    runs_dir = Path(runs_dir) if runs_dir is not None else runs_root()
    run_dir = runs_dir / run_id_for(session_id)
    name = event.get("hook_event_name")
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd")
    if name not in ("SessionStart", "PostToolUse", "PostToolUseFailure", "SessionEnd"):
        return None
    if not cwd or not Path(cwd).is_dir():
        _log(run_dir, f"ignored {name}: no usable cwd ({cwd!r})")
        return None
    workdir = Path(cwd).resolve()

    with _locked(run_dir):
        meta = _ensure_run(run_dir, session_id, workdir, note_late=name != "SessionStart")
        if name == "SessionStart":
            return run_dir
        if name == "SessionEnd":
            meta.finished = True
            _meta_path(run_dir).write_text(meta.model_dump_json(indent=2))
            return run_dir

        tool_use_id = event.get("tool_use_id")
        if _seen(run_dir, tool_use_id, add=True):
            return run_dir
        tool_name = str(event.get("tool_name") or "")
        kind = classify(tool_name)
        output, exit_code = parse_event(event)
        reasoning, tin, tout, model = from_transcript(event.get("transcript_path"), tool_use_id)
        index = _step_count(run_dir) + 1
        snap = tree = None
        if should_snapshot(tool_name, kind):
            snap, tree = snapshot.take_snapshot(run_dir, meta.workdir, f"step {index}: {tool_name}")
        step = Step(
            index=index,
            tool=tool_name,
            kind=kind,
            args=_cap_args(event.get("tool_input")),
            output=output,
            exit_code=exit_code,
            reasoning=reasoning,
            snapshot=snap,
            tree=tree,
            tokens_in=tin,
            tokens_out=tout,
            duration_s=round(float(event.get("duration_ms") or 0) / 1000, 3),
        )
        with (run_dir / "trace.jsonl").open("a") as f:
            f.write(step.model_dump_json() + "\n")
        if model and not meta.model:
            meta.model = model
            _meta_path(run_dir).write_text(meta.model_dump_json(indent=2))
        return run_dir


def main(stdin: Any = None) -> int:
    """Entry point. Always returns 0."""
    root = runs_root()
    try:
        raw = (stdin or sys.stdin).read()
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception:
        _log_global(root, "bad hook input:\n" + traceback.format_exc())
        return 0
    try:
        handle_event(event, root)
    except BaseException:
        sid = str(event.get("session_id") or "")
        text = "error handling hook event:\n" + traceback.format_exc()
        if sid:
            _log(root / run_id_for(sid), text)
        else:
            _log_global(root, text)
    return 0


def _log_global(root: Path, message: str) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "hook-errors.log").open("a") as f:
            f.write(f"{datetime.now(UTC).isoformat()} {message}\n")
    except OSError:
        pass


SETTINGS_SNIPPET = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "<CMD>", "timeout": 60}]}],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "<CMD>", "timeout": 60}],
            }
        ],
        "PostToolUseFailure": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "<CMD>", "timeout": 60}],
            }
        ],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "<CMD>", "timeout": 60}]}],
    }
}


def settings_snippet() -> str:
    """The hooks block to merge into settings.json, with this checkout's Python filled in."""
    cmd = f'"{sys.executable}" -m agentbisect.hooks.claude_code'
    return json.dumps(SETTINGS_SNIPPET, indent=2).replace("<CMD>", cmd.replace('"', '\\"'))


if __name__ == "__main__":
    if "--print-settings" in sys.argv[1:]:
        print(settings_snippet())
        sys.exit(0)
    with contextlib.suppress(BaseException):  # never fail the user's session
        main()
    sys.exit(0)
