from pathlib import Path

from typer.testing import CliRunner

from agentbisect.cli import app
from agentbisect.models import Step, ToolKind, Trace, TraceMeta, TraceSource


def _trace() -> Trace:
    meta = TraceMeta(
        run_id="r1", source=TraceSource.SYNTHETIC, workdir=Path("/w"), base_snapshot="b0"
    )
    steps = [
        Step(
            index=1, tool="edit_file", kind=ToolKind.EDIT, snapshot="s1", tokens_in=10, tokens_out=5
        ),
        Step(index=2, tool="read_file", kind=ToolKind.READ, tokens_in=3),
        Step(index=3, tool="run_shell", kind=ToolKind.SHELL, snapshot="s3", exit_code=1),
    ]
    return Trace(meta=meta, steps=steps)


def test_snapshot_at_falls_back_to_previous_snapshot():
    t = _trace()
    assert t.snapshot_at(0) == "b0"
    assert t.snapshot_at(1) == "s1"
    assert t.snapshot_at(2) == "s1"
    assert t.snapshot_at(3) == "s3"
    assert t.total_tokens == 18


def test_trace_roundtrip(tmp_path: Path):
    t = _trace()
    t.save(tmp_path)
    assert Trace.load(tmp_path) == t


def test_cli_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("record", "run", "report", "study"):
        assert cmd in result.output
