import os
import sys
from pathlib import Path

import pytest

from agentbisect.harness.tools import (
    FALLBACK_PROTOCOL,
    TOOL_SCHEMAS,
    Toolbox,
    clip,
    shell_env,
    tool_kind,
)
from agentbisect.llm import parse_json_action
from agentbisect.models import ToolKind


@pytest.fixture
def box(tmp_path: Path) -> Toolbox:
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "a.txt").write_text("alpha\nbeta\nalpha\n")
    return Toolbox(wd, shell_timeout_s=5)


def test_schemas_are_openai_function_tools():
    names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    assert names == ["read_file", "write_file", "edit_file", "run_shell", "finish"]
    for t in TOOL_SCHEMAS:
        assert t["type"] == "function"
        params = t["function"]["parameters"]
        assert set(params["required"]) <= set(params["properties"])


def test_tool_kinds():
    assert tool_kind("read_file") is ToolKind.READ
    assert tool_kind("write_file") is ToolKind.WRITE
    assert tool_kind("edit_file") is ToolKind.EDIT
    assert tool_kind("run_shell") is ToolKind.SHELL
    assert tool_kind("finish") is ToolKind.FINISH
    assert tool_kind("rm_rf") is ToolKind.OTHER


def test_fallback_protocol_example_parses():
    block = FALLBACK_PROTOCOL.split("```json\n")[1].split("```")[0]
    call = parse_json_action("```json\n" + block + "```")
    assert call is not None and call.name == "<tool name>"


def test_read_write_roundtrip(box: Toolbox):
    assert box.execute("read_file", {"path": "a.txt"}).output == "alpha\nbeta\nalpha\n"
    res = box.execute("write_file", {"path": "sub/dir/b.txt", "content": "hi"})
    assert res.ok
    assert (box.workdir / "sub/dir/b.txt").read_text() == "hi"


def test_read_missing_file_is_an_error_string(box: Toolbox):
    res = box.execute("read_file", {"path": "nope.txt"})
    assert not res.ok and res.output.startswith("error:")


@pytest.mark.parametrize("bad", ["../x.txt", "/etc/passwd", "sub/../../x", "a.txt/../../x"])
def test_paths_cannot_escape(box: Toolbox, bad: str):
    for tool, args in (
        ("read_file", {"path": bad}),
        ("write_file", {"path": bad, "content": "x"}),
        ("edit_file", {"path": bad, "old": "a", "new": "b"}),
    ):
        res = box.execute(tool, args)
        assert not res.ok and "outside the working directory" in res.output
    assert not (box.workdir.parent / "x.txt").exists()


def test_symlink_out_of_workdir_is_refused(box: Toolbox, tmp_path: Path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (box.workdir / "link.txt").symlink_to(outside)
    res = box.execute("read_file", {"path": "link.txt"})
    assert not res.ok and "outside" in res.output


def test_absolute_path_inside_workdir_is_fine(box: Toolbox):
    assert box.execute("read_file", {"path": str(box.workdir / "a.txt")}).ok


def test_edit_needs_exactly_one_match(box: Toolbox):
    many = box.execute("edit_file", {"path": "a.txt", "old": "alpha", "new": "z"})
    assert not many.ok and "2 times" in many.output
    none = box.execute("edit_file", {"path": "a.txt", "old": "gamma", "new": "z"})
    assert not none.ok and "not found" in none.output
    assert (box.workdir / "a.txt").read_text() == "alpha\nbeta\nalpha\n"
    ok = box.execute("edit_file", {"path": "a.txt", "old": "beta", "new": "BETA"})
    assert ok.ok
    assert (box.workdir / "a.txt").read_text() == "alpha\nBETA\nalpha\n"


def test_edit_rejects_empty_old(box: Toolbox):
    assert not box.execute("edit_file", {"path": "a.txt", "old": "", "new": "x"}).ok


def test_bad_arguments_return_errors_not_exceptions(box: Toolbox):
    assert not box.execute("read_file", {}).ok
    assert not box.execute("write_file", {"path": "x.txt"}).ok
    assert not box.execute("edit_file", {"path": "a.txt", "old": 3, "new": "x"}).ok
    assert not box.execute("run_shell", {"command": ["ls"]}).ok
    res = box.execute("nope", {})
    assert not res.ok and "unknown tool" in res.output


def test_shell_captures_output_and_exit_code(box: Toolbox):
    res = box.execute("run_shell", {"command": "echo out; echo err >&2; exit 3"})
    assert res.exit_code == 3
    assert "out" in res.output and "err" in res.output
    assert box.execute("run_shell", {"command": "true"}).exit_code == 0


def test_shell_runs_in_workdir_with_venv_python_first(box: Toolbox):
    res = box.execute(
        "run_shell", {"command": "pwd; python -c 'import sys; print(sys.executable)'"}
    )
    assert str(box.workdir) in res.output
    assert str(Path(sys.executable).parent) in res.output


def test_shell_timeout_kills_command(box: Toolbox):
    box.shell_timeout_s = 0.5
    res = box.execute("run_shell", {"command": "sleep 30"})
    assert res.exit_code == 124 and "timed out" in res.output


def test_shell_env_hides_alan_secrets(monkeypatch):
    monkeypatch.setenv("ALAN_API_KEY", "sk-test")
    env = shell_env()
    assert "ALAN_API_KEY" not in env
    assert env["PATH"].startswith(str(Path(sys.executable).parent) + os.pathsep)


def test_finish_sets_flag(box: Toolbox):
    res = box.execute("finish", {"summary": "done"})
    assert res.finished and "done" in res.output


def test_clip_keeps_tail_over_4000_chars():
    text = "H" * 5000 + "M" * 30000 + "T" * 9000
    out = clip(text)
    assert len(out) < len(text)
    assert out.endswith("T" * 8000)
    assert out.startswith("H" * 4000)
    assert clip("short") == "short"
