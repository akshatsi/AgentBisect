import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.bisect import bisect
from agentbisect.hooks import claude_code as hook
from agentbisect.models import BisectStatus, OracleConfig, ToolKind, Trace, TraceSource

ROOT = Path(__file__).resolve().parent.parent
SID = "sess-1"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (p / "check.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom app import add\nsys.exit(0 if add(1, 2) == 3 else 1)\n"
    )
    return p


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "runs"
    monkeypatch.setenv("AGENTBISECT_RUNS_DIR", str(r))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return r


def event(project: Path, name: str, tool: str | None = None, **extra) -> dict:
    e = {
        "session_id": SID,
        "transcript_path": "/nonexistent.jsonl",
        "cwd": str(project),
        "hook_event_name": name,
    }
    if tool:
        e["tool_name"] = tool
    e.update(extra)
    return e


def send(e: dict) -> int:
    return hook.main(io.StringIO(json.dumps(e)))


def play_session(project: Path) -> Path:
    """SessionStart, a read, a good edit, a bad edit (step 3), a failing check, the same check."""
    app = project / "src" / "app.py"
    send(event(project, "SessionStart", source="startup"))
    send(
        event(
            project,
            "PostToolUse",
            "Read",
            tool_input={"file_path": str(app)},
            tool_response={"type": "text", "file": {"content": app.read_text()}},
            tool_use_id="t1",
        )
    )
    app.write_text("def add(a, b):\n    '''Add.'''\n    return a + b\n")
    send(
        event(
            project,
            "PostToolUse",
            "Edit",
            tool_input={"file_path": str(app), "old_string": "def add", "new_string": "def add"},
            tool_response={"filePath": str(app)},
            tool_use_id="t2",
        )
    )
    app.write_text("def add(a, b):\n    '''Add.'''\n    return a - b\n")
    send(
        event(
            project,
            "PostToolUse",
            "Write",
            tool_input={"file_path": str(app), "content": app.read_text()},
            tool_response={"filePath": str(app), "type": "update"},
            tool_use_id="t3",
        )
    )
    send(
        event(
            project,
            "PostToolUse",
            "Grep",
            tool_input={"pattern": "add"},
            tool_response={"numFiles": 1},
            tool_use_id="t4",
        )
    )
    send(
        event(
            project,
            "PostToolUseFailure",
            "Bash",
            tool_input={"command": "python check.py"},
            error='Exit code 1\nTraceback (most recent call last):\n  File "src/app.py", line 3\nAssertionError',
            is_interrupt=False,
            tool_use_id="t5",
            duration_ms=1500,
        )
    )
    send(
        event(
            project,
            "PostToolUse",
            "Bash",
            tool_input={"command": "echo hi"},
            tool_response={"stdout": "hi\n", "stderr": "", "interrupted": False},
            tool_use_id="t6",
        )
    )
    return app


def test_session_becomes_a_valid_trace(project: Path, runs: Path):
    play_session(project)
    run_dir = runs / f"cc-{SID}"
    trace = Trace.load(run_dir)
    assert trace.meta.source is TraceSource.CLAUDE_CODE
    assert trace.meta.workdir == project.resolve()
    assert [s.index for s in trace.steps] == [1, 2, 3, 4, 5, 6]
    assert [s.tool for s in trace.steps] == ["Read", "Edit", "Write", "Grep", "Bash", "Bash"]
    assert [s.kind for s in trace.steps] == [
        ToolKind.READ,
        ToolKind.EDIT,
        ToolKind.WRITE,
        ToolKind.READ,
        ToolKind.SHELL,
        ToolKind.SHELL,
    ]
    assert [s.snapshot is not None for s in trace.steps] == [False, True, True, False, True, True]
    fail, ok = trace.steps[4], trace.steps[5]
    assert fail.exit_code == 1 and "AssertionError" in fail.output
    assert fail.duration_s == 1.5
    assert ok.exit_code == 0 and ok.output == "hi\n"
    # the base is the state before the first edit; the user's directory has no shadow files
    assert not (project / "shadow.git").exists() and not (project / ".git").exists()


def test_bisect_over_hook_trace_finds_the_bad_write(project: Path, runs: Path):
    play_session(project)
    run_dir = runs / f"cc-{SID}"
    trace = Trace.load(run_dir)
    # copy of the project is not needed: the oracle runs on restored snapshots
    result = bisect(trace, run_dir, OracleConfig(check=f"{shlex.quote(sys.executable)} check.py"))
    assert result.status is BisectStatus.FOUND, result.reason
    assert result.decisive_step == 3
    diff = snapshot.diff(run_dir, trace.snapshot_at(2), trace.snapshot_at(3))
    assert "-    return a + b" in diff and "+    return a - b" in diff


def test_duplicate_delivery_is_ignored(project: Path, runs: Path):
    app = play_session(project)
    n = Trace.load(runs / f"cc-{SID}").n
    send(
        event(
            project,
            "PostToolUse",
            "Edit",
            tool_input={"file_path": str(app)},
            tool_response={},
            tool_use_id="t2",
        )
    )
    assert Trace.load(runs / f"cc-{SID}").n == n


def test_first_event_without_session_start_still_works(project: Path, runs: Path):
    send(
        event(
            project,
            "PostToolUse",
            "Bash",
            tool_input={"command": "ls"},
            tool_response={"stdout": "x", "stderr": ""},
            tool_use_id="a",
        )
    )
    trace = Trace.load(runs / f"cc-{SID}")
    assert trace.n == 1
    assert "no SessionStart hook" in (runs / f"cc-{SID}" / "hook.log").read_text()


def test_session_end_marks_finished(project: Path, runs: Path):
    play_session(project)
    send(event(project, "SessionEnd", reason="other"))
    assert Trace.load(runs / f"cc-{SID}").meta.finished


def test_claude_project_dir_wins_over_cwd(project: Path, runs: Path, monkeypatch, tmp_path: Path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    send(event(other, "SessionStart"))
    meta = json.loads((runs / f"cc-{SID}" / "meta.json").read_text())
    assert Path(meta["workdir"]) == project.resolve()


def test_irrelevant_events_and_missing_session_are_ignored(project: Path, runs: Path):
    assert send(event(project, "UserPromptSubmit")) == 0
    assert send({"hook_event_name": "PostToolUse", "tool_name": "Bash"}) == 0
    assert not runs.exists() or not any(runs.iterdir())


def test_bad_input_never_fails(project: Path, runs: Path):
    assert hook.main(io.StringIO("not json")) == 0
    assert hook.main(io.StringIO("[1, 2]")) == 0
    assert hook.main(io.StringIO("")) == 0
    assert "bad hook input" in (runs / "hook-errors.log").read_text()


def test_internal_error_is_logged_and_exit_is_zero(project: Path, runs: Path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(hook.snapshot, "take_snapshot", boom)
    send(event(project, "SessionStart"))
    code = send(
        event(
            project,
            "PostToolUse",
            "Edit",
            tool_input={},
            tool_response={},
            tool_use_id="z",
        )
    )
    assert code == 0
    assert "shadow exploded" in (runs / f"cc-{SID}" / "hook.log").read_text()


def test_tokens_and_reasoning_from_transcript(project: Path, runs: Path, tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "claude-test",
                "content": [{"type": "text", "text": "I will fix the bug."}],
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 90,
                    "cache_read_input_tokens": 400,
                    "output_tokens": 50,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "claude-test",
                "content": [{"type": "tool_use", "id": "toolu_A", "name": "Edit", "input": {}}],
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 90,
                    "cache_read_input_tokens": 400,
                    "output_tokens": 50,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": "claude-test",
                "content": [{"type": "tool_use", "id": "toolu_B", "name": "Edit", "input": {}}],
                "usage": {"input_tokens": 10, "output_tokens": 50},
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    send(event(project, "SessionStart"))
    for tid in ("toolu_A", "toolu_B"):
        send(
            event(
                project,
                "PostToolUse",
                "Edit",
                transcript_path=str(transcript),
                tool_input={},
                tool_response={},
                tool_use_id=tid,
            )
        )
    trace = Trace.load(runs / f"cc-{SID}")
    a, b = trace.steps
    assert a.reasoning == "I will fix the bug."
    assert (a.tokens_in, a.tokens_out) == (500, 50)
    # second call of the same model response: no double counting
    assert (b.tokens_in, b.tokens_out) == (0, 0)
    assert trace.meta.model == "claude-test"


def test_parse_event_shapes():
    out, code = hook.parse_event(
        {"tool_name": "Bash", "tool_response": {"stdout": "a", "stderr": "b", "interrupted": False}}
    )
    assert (out, code) == ("ab", 0)
    out, code = hook.parse_event(
        {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "error": "Exit code 2\nboom"}
    )
    assert (out, code) == ("boom", 2)
    out, code = hook.parse_event(
        {"hook_event_name": "PostToolUseFailure", "tool_name": "Edit", "error": "no match"}
    )
    assert (out, code) == ("no match", None)
    _, code = hook.parse_event(
        {"tool_name": "Bash", "tool_response": {"stdout": "", "interrupted": True}}
    )
    assert code is None


def test_classify():
    assert hook.classify("Write") is ToolKind.WRITE
    for t in ("Edit", "MultiEdit", "NotebookEdit"):
        assert hook.classify(t) is ToolKind.EDIT
    assert hook.classify("Bash") is ToolKind.SHELL
    for t in ("Read", "Grep", "Glob"):
        assert hook.classify(t) is ToolKind.READ
    assert hook.classify("WebFetch") is ToolKind.OTHER
    assert hook.should_snapshot("Task", ToolKind.OTHER)
    assert hook.should_snapshot("mcp__x__y", ToolKind.OTHER)
    assert not hook.should_snapshot("WebFetch", ToolKind.OTHER)
    assert not hook.should_snapshot("Read", ToolKind.READ)


def test_runs_as_a_module_and_exits_zero(project: Path, tmp_path: Path):
    env = {**os.environ, "AGENTBISECT_RUNS_DIR": str(tmp_path / "r2")}
    env.pop("CLAUDE_PROJECT_DIR", None)
    for payload in (json.dumps(event(project, "SessionStart")), "garbage"):
        proc = subprocess.run(
            [sys.executable, "-m", "agentbisect.hooks.claude_code"],
            input=payload,
            capture_output=True,
            text=True,
            cwd=project,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0
        assert proc.stdout == ""
    assert (tmp_path / "r2" / f"cc-{SID}" / "meta.json").exists()


def test_settings_snippet_registers_all_events():
    snippet = json.loads(hook.settings_snippet())
    assert set(snippet["hooks"]) == {
        "SessionStart",
        "PostToolUse",
        "PostToolUseFailure",
        "SessionEnd",
    }
    cmd = snippet["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert cmd.endswith("-m agentbisect.hooks.claude_code") and sys.executable in cmd
