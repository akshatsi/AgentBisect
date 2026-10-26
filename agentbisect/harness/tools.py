"""Tools the agent under test can call, all confined to one working directory.

Tools: ``read_file``, ``write_file``, ``edit_file``, ``run_shell``, ``finish``.

* Paths are resolved against the workdir. A path that leaves it (``..``, an absolute path
  elsewhere, a symlink that points out) is refused.
* ``edit_file`` is an exact find/replace and fails unless ``old`` occurs exactly once.
* ``run_shell`` runs in the workdir with a timeout, merges stdout and stderr, and puts the
  current Python's bin directory first on ``PATH`` so ``python -m pytest`` works. ``ALAN_*``
  variables are removed from its environment, so the agent cannot read the API key.

Every tool returns a ``ToolResult``. Errors come back as text for the agent to read; the harness
never raises on a bad tool call.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbisect.models import ToolKind

DEFAULT_SHELL_TIMEOUT_S = 60.0
#: Longest tool output kept. Over this, the middle is cut; the tail (where pytest prints its
#: summary) stays whole and is always well over 4000 characters.
MAX_OUTPUT_CHARS = 12_000
MAX_READ_BYTES = 400_000

TOOL_KINDS: dict[str, ToolKind] = {
    "read_file": ToolKind.READ,
    "write_file": ToolKind.WRITE,
    "edit_file": ToolKind.EDIT,
    "run_shell": ToolKind.SHELL,
    "finish": ToolKind.FINISH,
}


def tool_kind(name: str) -> ToolKind:
    return TOOL_KINDS.get(name, ToolKind.OTHER)


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_STR = {"type": "string"}

#: Tool schemas in the OpenAI function-tool format.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "read_file",
        "Read a text file in the working directory and return its contents.",
        {"path": {**_STR, "description": "File path relative to the working directory."}},
        ["path"],
    ),
    _fn(
        "write_file",
        "Create a file, or replace it completely. Parent directories are created.",
        {
            "path": {**_STR, "description": "File path relative to the working directory."},
            "content": {**_STR, "description": "The full new contents of the file."},
        },
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace one exact piece of text in a file. Fails unless `old` occurs exactly once.",
        {
            "path": {**_STR, "description": "File path relative to the working directory."},
            "old": {
                **_STR,
                "description": "Exact text to find. Include enough context to be unique.",
            },
            "new": {**_STR, "description": "Text to put in its place."},
        },
        ["path", "old", "new"],
    ),
    _fn(
        "run_shell",
        "Run a shell command in the working directory. Returns its combined output and exit code.",
        {"command": {**_STR, "description": "The command line, e.g. `python -m pytest -q`."}},
        ["command"],
    ),
    _fn(
        "finish",
        "Call this when the task is complete (or you cannot make further progress).",
        {"summary": {**_STR, "description": "One or two sentences on what you did."}},
        ["summary"],
    ),
]

#: Appended to the system prompt for models without native tool calls (see llm.parse_json_action).
FALLBACK_PROTOCOL = """\
You have no native tool calls. To use a tool, reply with a short note on what you are doing, then
exactly one fenced JSON block of this form:

```json
{"tool": "<tool name>", "args": {"<argument>": "<value>"}}
```

Tools and their arguments:
- read_file: {"path": str}
- write_file: {"path": str, "content": str}
- edit_file: {"path": str, "old": str, "new": str}   (`old` must occur exactly once)
- run_shell: {"command": str}
- finish: {"summary": str}

Use one tool per reply. The result comes back in the next message."""


@dataclass
class ToolResult:
    output: str
    exit_code: int | None = None
    ok: bool = True
    #: Set by finish: the agent says it is done.
    finished: bool = False


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Cut the middle of long text. Keeps one third of the budget at the head, two thirds at the tail."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n... [{cut} characters cut] ...\n{text[-tail:]}"


def shell_env() -> dict[str, str]:
    """Environment for agent shell commands: venv bin first, no Alan secrets, no bytecode."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALAN_")}
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = ("-p no:cacheprovider " + env.get("PYTEST_ADDOPTS", "")).strip()
    return env


class PathEscape(ValueError):
    pass


class Toolbox:
    """Runs tool calls against one workdir."""

    def __init__(
        self,
        workdir: Path,
        shell_timeout_s: float = DEFAULT_SHELL_TIMEOUT_S,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.workdir = Path(workdir).resolve()
        self.shell_timeout_s = shell_timeout_s
        self.max_output_chars = max_output_chars
        self._handlers: dict[str, Callable[[Mapping[str, Any]], ToolResult]] = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "run_shell": self._run_shell,
            "finish": self._finish,
        }

    # -- dispatch ----------------------------------------------------------

    def execute(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            known = ", ".join(self._handlers)
            return ToolResult(f"error: unknown tool '{name}'. Available tools: {known}", ok=False)
        try:
            result = handler(args)
        except (PathEscape, OSError, ValueError, TypeError) as e:
            return ToolResult(f"error: {e}", ok=False)
        result.output = clip(result.output, self.max_output_chars)
        return result

    # -- helpers -----------------------------------------------------------

    def resolve(self, path: Any) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("'path' must be a non-empty string")
        full = (self.workdir / path).resolve()
        if full != self.workdir and self.workdir not in full.parents:
            raise PathEscape(f"path '{path}' is outside the working directory")
        return full

    @staticmethod
    def _need_str(args: Mapping[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string")
        return value

    # -- tools -------------------------------------------------------------

    def _read_file(self, args: Mapping[str, Any]) -> ToolResult:
        path = self.resolve(args.get("path"))
        if not path.is_file():
            return ToolResult(f"error: no such file: {args['path']}", ok=False)
        data = path.read_bytes()[:MAX_READ_BYTES]
        return ToolResult(data.decode(errors="replace"))

    def _write_file(self, args: Mapping[str, Any]) -> ToolResult:
        path = self.resolve(args.get("path"))
        content = self._need_str(args, "content")
        if path.is_dir():
            return ToolResult(f"error: {args['path']} is a directory", ok=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return ToolResult(f"wrote {len(content)} characters to {args['path']}")

    def _edit_file(self, args: Mapping[str, Any]) -> ToolResult:
        path = self.resolve(args.get("path"))
        old = self._need_str(args, "old")
        new = self._need_str(args, "new")
        if not old:
            return ToolResult("error: 'old' must not be empty", ok=False)
        if not path.is_file():
            return ToolResult(f"error: no such file: {args['path']}", ok=False)
        text = path.read_text()
        count = text.count(old)
        if count == 0:
            return ToolResult(f"error: 'old' text not found in {args['path']}", ok=False)
        if count > 1:
            return ToolResult(
                f"error: 'old' text occurs {count} times in {args['path']}; "
                "add more context so it matches exactly once",
                ok=False,
            )
        path.write_text(text.replace(old, new, 1))
        return ToolResult(f"edited {args['path']}")

    def _run_shell(self, args: Mapping[str, Any]) -> ToolResult:
        command = self._need_str(args, "command")
        return run_command(command, self.workdir, self.shell_timeout_s)

    def _finish(self, args: Mapping[str, Any]) -> ToolResult:
        summary = args.get("summary", "")
        return ToolResult(f"finished: {summary}" if summary else "finished", finished=True)


def run_command(command: str, cwd: Path, timeout_s: float) -> ToolResult:
    """Run ``command`` through the shell. Timeout kills the whole process group (exit code 124)."""
    start = time.monotonic()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=shell_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        text = out.decode(errors="replace") if out else ""
        elapsed = time.monotonic() - start
        return ToolResult(
            f"{text}\n[timed out after {elapsed:.0f}s; command killed]".lstrip("\n"),
            exit_code=124,
            ok=False,
        )
    text = out.decode(errors="replace") if out else ""
    code = proc.returncode
    return ToolResult(text if text.strip() else f"(no output, exit code {code})", exit_code=code)
