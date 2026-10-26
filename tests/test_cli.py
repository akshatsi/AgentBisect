"""The four CLI commands: help, exit codes and output. No network, no credits."""

from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentbisect import llm
from agentbisect.cli import app
from agentbisect.llm import FakeLLM
from agentbisect.models import TraceMeta, TraceSource
from agentbisect.synthetic import make_synthetic_run

CHECK = "python -m pytest -q tests/test_calc.py::test_add"
PROJECT = Path(__file__).resolve().parents[1]

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args), env={"COLUMNS": "120"})


FAST_CHECK = (
    'python -c "import pathlib, sys; '
    "sys.exit(1 if 'a - b' in pathlib.Path('calc/__init__.py').read_text() else 0)\""
)


@pytest.fixture(scope="module")
def bisected(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic run that `agentbisect run` has bisected once. Tests copy it."""
    run = tmp_path_factory.mktemp("cli") / "run"
    make_synthetic_run(run, n_steps=10, break_at=4, observe_at=7)
    result = invoke("run", "--trace", str(run), "--check", CHECK)
    assert result.exit_code == 0, result.output
    return run


@pytest.fixture
def synthetic(bisected: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "run"
    shutil.copytree(bisected, dest)
    return dest


# --------------------------------------------------------------------------- help


def test_exactly_four_commands() -> None:
    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert names == {"record", "run", "report", "study"}
    result = invoke("--help")
    assert result.exit_code == 0
    for name in names:
        assert name in result.output


# --------------------------------------------------------------------------- run


def test_run_prints_verdict_and_writes_bisect_json(synthetic: Path) -> None:
    (synthetic / "bisect.json").unlink()
    result = invoke("run", "--trace", str(synthetic), "--check", FAST_CHECK)
    assert result.exit_code == 0, result.output
    assert "Step 4 (edit calc/__init__.py) broke" in result.output
    assert "The agent only noticed at step 7" in result.output
    assert "Probes (" in result.output
    data = json.loads((synthetic / "bisect.json").read_text())
    assert data["culprits"][0]["bisect"]["decisive_step"] == 4
    assert data["culprits"][0]["analysis"]["lag"] == 3


def test_run_auto_oracle_finds_newly_failing_test(synthetic: Path) -> None:
    result = invoke("run", "--trace", str(synthetic))
    assert result.exit_code == 0, result.output
    assert "Step 4" in result.output and "test_add" in result.output
    assert json.loads((synthetic / "bisect.json").read_text())["auto_oracle"] is True


def test_run_exits_1_when_not_bisectable(synthetic: Path) -> None:
    result = invoke("run", "--trace", str(synthetic), "--check", "true")
    assert result.exit_code == 1
    assert "Not bisectable" in result.output
    assert "Traceback" not in result.output


def test_run_missing_trace_exits_2_cleanly(tmp_path: Path) -> None:
    result = invoke("run", "--trace", str(tmp_path / "nope"), "--check", "true")
    assert result.exit_code == 2
    assert "No trace" in result.output
    assert "Traceback" not in result.output


def test_run_accepts_a_relative_trace_path(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(synthetic.parent)
    result = invoke("run", "--trace", synthetic.name, "--check", FAST_CHECK)
    assert result.exit_code == 0, result.output
    assert "Step 4" in result.output


# --------------------------------------------------------------------------- report


def test_report_writes_html(synthetic: Path) -> None:
    result = invoke("report", "--trace", str(synthetic))
    assert result.exit_code == 0, result.output
    html = (synthetic / "report.html").read_text()
    assert "Step 4 (edit calc/__init__.py) broke test_add." in html
    assert "AI explanation" not in html


def test_report_without_bisect_json_warns_but_renders(tmp_path: Path) -> None:
    run = tmp_path / "run"
    make_synthetic_run(run, n_steps=5, break_at=2)
    result = invoke("report", "--trace", str(run))
    assert result.exit_code == 0, result.output
    assert "No bisect.json yet" in result.output
    assert "Not bisected yet" in (run / "report.html").read_text()


def test_report_missing_trace_exits_2(tmp_path: Path) -> None:
    result = invoke("report", "--trace", str(tmp_path))
    assert result.exit_code == 2


def test_report_explain_without_credentials_still_writes(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def no_credentials(*_a: object, **_k: object) -> None:
        raise llm.ConfigError("ALAN_API_KEY is not set")

    monkeypatch.setattr(llm, "create_client", no_credentials)
    fake = types.ModuleType("agentbisect.explain")
    fake.explain = lambda culprit, client: "never called"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentbisect.explain", fake)
    result = invoke("report", "--trace", str(synthetic), "--explain")
    assert result.exit_code == 0, result.output
    assert "No AI explanation" in result.output
    assert "AI explanation" not in (synthetic / "report.html").read_text()


def test_report_explain_adds_labelled_box(synthetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "create_client", lambda *_a, **_k: FakeLLM([], model="fake"))
    fake = types.ModuleType("agentbisect.explain")
    fake.explain = lambda culprit, client: "The edit flipped the sign."  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentbisect.explain", fake)
    result = invoke("report", "--trace", str(synthetic), "--explain")
    assert result.exit_code == 0, result.output
    html = (synthetic / "report.html").read_text()
    assert "AI explanation" in html and "The edit flipped the sign." in html


# --------------------------------------------------------------------------- record


def install_fake_opencode(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Stand in for agentbisect.hooks.opencode so no real run folder logic is needed."""
    calls: list[tuple] = []
    fake = types.ModuleType("agentbisect.hooks.opencode")

    def prepare_opencode_run(task, runs_dir, model_alias=None, run_id=None):  # noqa: ANN001
        calls.append((task.id, model_alias, run_id))
        run = Path(runs_dir) / (run_id or "alan-run")
        (run / "workdir").mkdir(parents=True)
        meta = TraceMeta(
            run_id=run.name,
            source=TraceSource.ALAN_CODE,
            workdir=run / "workdir",
            base_snapshot="0" * 40,
            task_id=task.id,
        )
        (run / "meta.json").write_text(meta.model_dump_json())
        (run / "PROMPT.txt").write_text("Add Catalog.update_price please.\n")
        return run

    fake.prepare_opencode_run = prepare_opencode_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentbisect.hooks.opencode", fake)
    return calls


def test_record_missing_task_exits_2(tmp_path: Path) -> None:
    result = invoke("record", "--task", str(tmp_path / "nope.yaml"))
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_record_default_prints_alan_code_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_fake_opencode(monkeypatch)
    result = invoke(
        "record",
        "--task",
        str(PROJECT / "tasks" / "t01.yaml"),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        "ac1",
        "--model",
        "flash",
    )
    assert result.exit_code == 0, result.output
    assert calls == [("t01", "flash", "ac1")]
    assert "1. Open the Alan app" in result.output
    assert str(tmp_path / "ac1" / "workdir") in result.output
    assert "DeepSeek V4.1 Flash" in result.output or "alias flash" in result.output
    assert "PROMPT.txt" in result.output
    assert "Add Catalog.update_price please." in result.output
    assert "agentbisect run --trace" in result.output


def test_record_default_needs_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_opencode(monkeypatch)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the Alan Code flow must not create an LLM client")

    monkeypatch.setattr(llm, "create_client", boom)
    result = invoke(
        "record", "--task", str(PROJECT / "tasks" / "t01.yaml"), "--runs-dir", str(tmp_path)
    )
    assert result.exit_code == 0, result.output


def test_record_harness_without_credentials_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_credentials(*_a: object, **_k: object) -> None:
        raise llm.ConfigError("ALAN_API_KEY is not set")

    monkeypatch.setattr(llm, "create_client", no_credentials)
    result = invoke(
        "record",
        "--harness",
        "--task",
        str(PROJECT / "tasks" / "t01.yaml"),
        "--runs-dir",
        str(tmp_path),
    )
    assert result.exit_code == 2
    assert "Cannot start the model client" in result.output
    assert not list(tmp_path.iterdir())


def test_record_harness_prints_model_and_cap_then_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PROJECT / "tests" / "fixtures" / "fake_run_t01.jsonl"
    client = FakeLLM.from_jsonl(fixture, model="fake")
    client.config = types.SimpleNamespace(spend_cap_usd=8.0)  # type: ignore[attr-defined]
    monkeypatch.setattr(llm, "create_client", lambda *_a, **_k: client)
    result = invoke(
        "record",
        "--harness",
        "--task",
        str(PROJECT / "tasks" / "t01.yaml"),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        "cli-fake",
    )
    assert result.exit_code == 0, result.output
    assert "fake" in result.output and "$8.00 hard spend cap" in result.output
    assert (tmp_path / "cli-fake" / "trace.jsonl").exists()
    assert "Next: agentbisect run" in result.output


# --------------------------------------------------------------------------- study


def test_study_is_a_clear_stub() -> None:
    result = invoke("study", "--models", "qwen", "--repeats", "2")
    assert result.exit_code == 2
    assert "M3" in result.output
