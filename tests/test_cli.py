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
from agentbisect.models import StudySummary, TraceMeta, TraceSource
from agentbisect.synthetic import make_synthetic_run
from tests.test_study_report import make_summary

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


# --------------------------------------------------------------------------- record --repo


class FakeSetupError(Exception):
    def __init__(self, message: str, log_path: Path) -> None:
        super().__init__(f"{message} (see {log_path})")
        self.log_path = log_path


def install_fake_repo_flow(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[dict]:
    """Stand in for prepare_repo_run (and SetupError) so no clone or install happens."""
    calls: list[dict] = []
    try:
        from agentbisect.workspace import SetupError
    except ImportError:
        workspace = types.ModuleType("agentbisect.workspace")
        workspace.SetupError = FakeSetupError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agentbisect.workspace", workspace)
        SetupError = FakeSetupError  # noqa: N806

    mod = types.ModuleType("agentbisect.hooks.opencode")

    def prepare_repo_run(  # noqa: ANN202
        origin,
        prompt,
        runs_dir,
        *,
        model_alias=None,
        run_id=None,
        ref=None,
        setup=True,
        sabotage=None,
    ):
        calls.append(
            dict(
                origin=origin,
                prompt=prompt,
                model_alias=model_alias,
                run_id=run_id,
                ref=ref,
                setup=setup,
                sabotage=sabotage,
            )
        )
        run = Path(runs_dir) / (run_id or "repo-run")
        (run / "workdir").mkdir(parents=True)
        if fail:
            raise SetupError("pip install failed", run / "setup.log")
        meta = TraceMeta(
            run_id=run.name,
            source=TraceSource.ALAN_CODE,
            workdir=run / "workdir",
            base_snapshot="0" * 40,
            repo_origin=origin,
        )
        (run / "meta.json").write_text(meta.model_dump_json())
        (run / "PROMPT.txt").write_text(prompt)
        return run

    mod.prepare_repo_run = prepare_repo_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentbisect.hooks.opencode", mod)
    return calls


def test_record_needs_exactly_one_of_task_and_repo(tmp_path: Path) -> None:
    neither = invoke("record", "--runs-dir", str(tmp_path))
    assert neither.exit_code == 2
    assert "exactly one of --task and --repo" in neither.output
    both = invoke(
        "record",
        "--task",
        str(PROJECT / "tasks" / "t01.yaml"),
        "--repo",
        str(tmp_path),
        "--prompt",
        "x",
    )
    assert both.exit_code == 2
    assert "exactly one of --task and --repo" in both.output


def test_record_repo_needs_exactly_one_prompt(tmp_path: Path) -> None:
    none = invoke("record", "--repo", str(tmp_path), "--runs-dir", str(tmp_path / "r"))
    assert none.exit_code == 2
    assert "--prompt" in none.output and "--prompt-file" in none.output
    pfile = tmp_path / "p.txt"
    pfile.write_text("hello")
    both = invoke("record", "--repo", str(tmp_path), "--prompt", "x", "--prompt-file", str(pfile))
    assert both.exit_code == 2
    empty = invoke("record", "--repo", str(tmp_path), "--prompt", "   ")
    assert empty.exit_code == 2 and "empty" in empty.output


def test_record_repo_rejects_harness(tmp_path: Path) -> None:
    result = invoke("record", "--repo", str(tmp_path), "--prompt", "x", "--harness")
    assert result.exit_code == 2
    assert "--harness works with --task only" in result.output
    assert "Traceback" not in result.output


def test_record_task_rejects_repo_only_flags() -> None:
    task = str(PROJECT / "tasks" / "t01.yaml")
    for extra in (["--prompt", "x"], ["--ref", "main"], ["--no-setup"]):
        result = invoke("record", "--task", task, *extra)
        assert result.exit_code == 2, extra
        assert "works with --repo only" in result.output


def test_record_repo_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_repo_flow(monkeypatch)
    pfile = tmp_path / "prompt.txt"
    pfile.write_text("Add a retry to fetch().\n")
    result = invoke(
        "record",
        "--repo",
        "https://github.com/me/proj",
        "--prompt-file",
        str(pfile),
        "--ref",
        "dev",
        "--no-setup",
        "--model",
        "flash",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--run-id",
        "r1",
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        dict(
            origin="https://github.com/me/proj",
            prompt="Add a retry to fetch().\n",
            model_alias="flash",
            run_id="r1",
            ref="dev",
            setup=False,
            sabotage=None,
        )
    ]
    assert "1. Open the Alan app" in result.output
    assert str(tmp_path / "runs" / "r1" / "workdir") in result.output
    assert "PROMPT.txt" in result.output
    assert "agentbisect run --trace" in result.output
    assert "original repo is not modified" in result.output
    assert "Add a retry to fetch()." in result.output


def test_record_repo_defaults_run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_repo_flow(monkeypatch)
    result = invoke(
        "record", "--repo", str(tmp_path), "--prompt", "do it", "--runs-dir", str(tmp_path / "r")
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["setup"] is True and calls[0]["ref"] is None


def test_record_repo_setup_error_prints_message_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_repo_flow(monkeypatch, fail=True)
    result = invoke(
        "record",
        "--repo",
        str(tmp_path),
        "--prompt",
        "x",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--run-id",
        "bad",
    )
    assert result.exit_code == 1
    assert "Setup failed" in result.output
    assert "pip install failed" in result.output
    assert "setup.log" in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------- study


def fake_study_module(summary: StudySummary) -> types.ModuleType:
    """A stand-in for agentbisect.study: the CLI is tested apart from the real aggregator."""
    mod = types.ModuleType("agentbisect.study")
    mod.calls = []  # type: ignore[attr-defined]

    def collect(runs_dir: Path, *, rebisect: bool = False) -> StudySummary:
        mod.calls.append(("collect", Path(runs_dir), rebisect))  # type: ignore[attr-defined]
        return summary.model_copy(deep=True)

    def summarize(s: StudySummary) -> StudySummary:
        return s.model_copy(update={"metrics": make_summary().metrics})

    def write(s: StudySummary, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "study.json"
        path.write_text(s.model_dump_json(indent=2))
        return path

    def keep_created_at(s: StudySummary, out_dir: Path) -> StudySummary:
        return s

    mod.collect, mod.summarize, mod.write = collect, summarize, write  # type: ignore[attr-defined]
    mod.keep_created_at = keep_created_at  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def study_runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    return runs


def test_study_missing_runs_folder_exits_2(tmp_path: Path) -> None:
    result = invoke("study", "--runs", str(tmp_path / "nope"))
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_study_empty_runs_folder_says_so_and_exits_1(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(StudySummary()))
    result = invoke("study", "--runs", str(study_runs))
    assert result.exit_code == 1
    assert "No usable runs" in result.output
    assert not (study_runs / "study").exists()


def test_study_empty_runs_folder_with_the_real_module(study_runs: Path) -> None:
    pytest.importorskip("agentbisect.study")
    result = invoke("study", "--runs", str(study_runs))
    assert result.exit_code == 1
    assert "No usable runs" in result.output


def test_study_lists_exclusions_when_nothing_is_usable(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = StudySummary(excluded={"t04-1": "resumed an earlier conversation"})
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(summary))
    result = invoke("study", "--runs", str(study_runs))
    assert result.exit_code == 1
    assert "t04-1" in result.output and "resumed an earlier conversation" in result.output


def test_study_writes_json_html_and_draft_and_prints_tables(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_study_module(make_summary())
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake)
    result = invoke("study", "--runs", str(study_runs), "--rebisect")
    assert result.exit_code == 0, result.output
    assert fake.calls == [("collect", study_runs.resolve(), True)]  # type: ignore[attr-defined]
    out = study_runs / "study"
    for name in ("study.json", "study.html", "RESULTS.draft.md"):
        assert (out / name).exists(), name
    for needle in (
        "Runs",
        "t04",
        "censored",
        "Excluded runs",
        "Alan resumed an earlier conversation",
        "Repaired breaks",
        "Metrics",
        "broken-run share",
        "The audited results are in docs/RESULTS.md",
    ):
        assert needle in result.output, needle
    assert "pending" not in result.output
    # No run folder exists on disk, so the page links to none of them.
    assert "report.html" not in (out / "study.html").read_text()


def test_study_links_to_runs_that_have_a_report(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_synthetic_run(study_runs / "t04-flash-1", n_steps=10, break_at=4, observe_at=7)
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(make_summary()))
    result = invoke("study", "--runs", str(study_runs))
    assert result.exit_code == 0, result.output
    assert (study_runs / "t04-flash-1" / "report.html").exists()
    html = (study_runs / "study" / "study.html").read_text()
    assert 'href="../t04-flash-1/report.html"' in html
    assert "../t08-flash-1/report.html" not in html


def test_study_models_filter(study_runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = make_summary()
    summary.rows[1].model = "pro"
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(summary))
    result = invoke("study", "--runs", str(study_runs), "--models", "pro")
    assert result.exit_code == 0, result.output
    data = json.loads((study_runs / "study" / "study.json").read_text())
    assert [r["run_id"] for r in data["rows"]] == ["t04-flash-1"]
    assert all(e["run_id"] == "t04-flash-1" for e in data["episodes"])

    none = invoke("study", "--runs", str(study_runs), "--models", "qwen")
    assert none.exit_code == 1 and "No usable runs" in none.output


def test_study_suite_filter(
    study_runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "t04.yaml").write_text("id: t04\n")
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(make_summary()))
    result = invoke("study", "--runs", str(study_runs), "--suite", str(suite))
    assert result.exit_code == 0, result.output
    data = json.loads((study_runs / "study" / "study.json").read_text())
    assert {r["task_id"] for r in data["rows"]} == {"t04"}


def test_study_accepts_the_old_repeats_flag(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "agentbisect.study", fake_study_module(make_summary()))
    result = invoke("study", "--runs", str(study_runs), "--repeats", "3")
    assert result.exit_code == 0, result.output
    help_text = invoke("study", "--help").output
    assert "--repeats" in help_text and "ignored" in help_text  # documented, not an option row
    assert "--repeats  " not in help_text


def test_study_without_the_study_module_exits_1(
    study_runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "agentbisect.study", None)
    result = invoke("study", "--runs", str(study_runs))
    assert result.exit_code == 1
    assert "not available" in result.output


# --------------------------------------------------------------------------- repaired breaks


def _repaired_report(run: Path, *, cap: bool = False):
    from agentbisect.bisect import CAP_NOTE
    from agentbisect.models import RepairEpisode, RunReport

    episode = RepairEpisode(
        run_id="r", check=CHECK, seen_at=7, t_err=4, lag=3, wasted_tokens=112_000
    )
    checks = [CHECK] + ([CHECK.replace("test_add", "test_other") + CAP_NOTE] if cap else [])
    return RunReport(run_id="r", checks=checks, repaired=[episode])


def test_run_prints_repaired_episode_and_exits_0(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentbisect.bisect.bisect_run", lambda *a, **k: _repaired_report(synthetic)
    )
    result = invoke("run", "--trace", str(synthetic))
    assert result.exit_code == 0, result.output
    assert "Step 4 (edit calc/__init__.py) broke test_add." in result.output
    assert "The agent saw it at step 7 and fixed it before the end." in result.output
    assert "3 steps and 112k tokens went into broken code first." in result.output
    assert "No failing check" not in result.output
    assert "Next: agentbisect report" in result.output
    data = json.loads((synthetic / "bisect.json").read_text())
    assert data["repaired"][0]["t_err"] == 4


def test_run_shows_cap_checks_and_untraceable_episode(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = _repaired_report(synthetic, cap=True)
    rep.repaired[0].t_err = None
    rep.repaired[0].reason = "the base already failed"
    monkeypatch.setattr("agentbisect.bisect.bisect_run", lambda *a, **k: rep)
    result = invoke("run", "--trace", str(synthetic))
    assert result.exit_code == 0, result.output
    assert "test_other: not bisected (cap)" in result.output
    assert "was not found: the base already failed" in result.output


def test_run_nothing_at_all_keeps_old_message_and_exits_1(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentbisect.models import RunReport

    monkeypatch.setattr(
        "agentbisect.bisect.bisect_run", lambda *a, **k: RunReport(run_id="r", checks=[])
    )
    result = invoke("run", "--trace", str(synthetic))
    assert result.exit_code == 1
    assert "No failing check to bisect" in result.output


def test_record_repo_passes_sabotage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_repo_flow(monkeypatch)
    sab = tmp_path / "sab.yaml"
    sab.write_text("at_step: 5\nfile: src/x.py\nfind: 'a + b'\nreplace: 'a - b'\n")
    result = invoke(
        "record", "--repo", "/some/proj", "--prompt", "go", "--sabotage", str(sab),
        "--runs-dir", str(tmp_path / "runs"),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert calls[0]["sabotage"].at_step == 5
    assert calls[0]["sabotage"].replace == "a - b"


def test_record_task_rejects_sabotage_flag(tmp_path: Path) -> None:
    sab = tmp_path / "sab.yaml"
    sab.write_text("at_step: 1\nfile: a\nfind: b\nreplace: c\n")
    result = invoke("record", "--task", "tasks/t01.yaml", "--sabotage", str(sab))
    assert result.exit_code != 0
    assert "works with --repo only" in result.output


# --------------------------------------------------------------------------- grouped verdicts


def _grouped_report(run: Path, steps: list[int], *, last_step: bool = False):
    """One culprit per entry of ``steps``, each a different test broken at that step."""
    from agentbisect.models import (
        Analysis,
        BisectResult,
        BisectStatus,
        CulpritReport,
        RunReport,
    )

    culprits = []
    for i, t in enumerate(steps):
        check = CHECK.replace("test_add", f"test_n[{i}]")
        analysis = Analysis(
            t_err=t,
            t_obs=None,
            lag=None,
            censored=True,
            steps_after_err=0 if last_step else 10 - t,
            wasted_tokens=0 if last_step else 5000,
            total_tokens=20_000,
        )
        b = BisectResult(check=check, status=BisectStatus.FOUND, decisive_step=t)
        culprits.append(CulpritReport(run_id="r", check=check, bisect=b, analysis=analysis))
    return RunReport(run_id="r", checks=[c.check for c in culprits], culprits=culprits)


def test_run_prints_one_box_for_culprits_at_the_same_step(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = _grouped_report(synthetic, [4, 4, 4])
    monkeypatch.setattr("agentbisect.bisect.bisect_run", lambda *a, **k: rep)
    result = invoke("run", "--trace", str(synthetic))
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert out.count("Verdict") == 1
    assert "Step 4 (edit calc/__init__.py) broke 3 tests: test_n[0], test_n[1], test_n[2]." in out
    assert out.count("The agent never noticed.") == 1


def test_run_prints_one_box_per_decisive_step(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = _grouped_report(synthetic, [4, 6, 4])
    monkeypatch.setattr("agentbisect.bisect.bisect_run", lambda *a, **k: rep)
    result = invoke("run", "--trace", str(synthetic))
    out = " ".join(result.output.split())
    assert out.count("Verdict") == 2
    assert "broke 2 tests: test_n[0], test_n[2]." in out
    assert "broke test_n[1]." in out


def test_run_says_when_the_break_was_the_last_step(
    synthetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rep = _grouped_report(synthetic, [10], last_step=True)
    monkeypatch.setattr("agentbisect.bisect.bisect_run", lambda *a, **k: rep)
    out = " ".join(invoke("run", "--trace", str(synthetic)).output.split())
    assert "This was the agent's last step, so it had no chance to see the failure." in out
    assert "The run ended with the test broken." in out
    assert "never noticed" not in out and "0 tokens" not in out


# --------------------------------------------------------------------------- replays and tables


def test_run_again_keeps_bisect_json_and_report_byte_identical(synthetic: Path) -> None:
    first = invoke("run", "--trace", str(synthetic), "--check", CHECK)
    assert first.exit_code == 0, first.output
    assert invoke("report", "--trace", str(synthetic)).exit_code == 0
    files = [synthetic / "bisect.json", synthetic / "report.html"]
    before = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
    again = invoke("run", "--trace", str(synthetic), "--check", CHECK)  # new created_at, durations
    assert again.exit_code == 0, again.output
    assert invoke("report", "--trace", str(synthetic)).exit_code == 0
    assert before == [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]


def test_run_rewrites_bisect_json_when_the_verdict_changes(synthetic: Path) -> None:
    before = (synthetic / "bisect.json").read_text()
    other = invoke("run", "--trace", str(synthetic), "--check", FAST_CHECK)
    assert other.exit_code == 0, other.output
    assert (synthetic / "bisect.json").read_text() != before


def _render_study_table(summary: StudySummary, width: int = 80) -> str:
    import io

    from rich.console import Console

    from agentbisect.cli import _print_study

    buf = io.StringIO()
    _print_study(Console(file=buf, width=width, highlight=False, force_terminal=False), summary)
    return buf.getvalue()


def _long_summary() -> StudySummary:
    from agentbisect.models import RepairEpisode, StudyRow

    def row(run_id: str, task: str, **kw: object) -> StudyRow:
        base = dict(
            run_id=run_id, task_id=task, model="flash", n_steps=21, final_failed=False,
            bisectable=False, total_tokens=1000,
        )  # fmt: skip
        return StudyRow(**{**base, **kw})  # type: ignore[arg-type]

    broke = dict(final_failed=True, bisectable=True, t_err=8, censored=True, wasted_tokens=10)
    long_test = "python -m pytest -q 'tests/test_filesize.py::test_naturalsize[test_args56-1.0K]'"
    rows = [
        row("t01-flash-20261026-095102", "t01"),
        row("caught-and-fixed", "s03", sabotage_step=9),
        *(
            row(
                "humanize-planted-break", "humanize", sabotage_step=8, sabotage_correct=True,
                check=long_test.replace("56", str(i)), **broke,
            )
            for i in range(3)
        ),
    ]  # fmt: skip
    ep = RepairEpisode(
        run_id="caught-and-fixed", check="python -m pytest -q tests/test_orders.py::" + "x" * 50,
        seen_at=13, t_err=9, lag=4, wasted_tokens=112179,
    )  # fmt: skip
    return make_summary().model_copy(
        update={
            "rows": rows,
            "episodes": [ep],
            "excluded": {"humanize-ordinal-fix": "a case study"},
        }
    )


def test_study_tables_fit_80_columns_without_splitting_ids() -> None:
    out = _render_study_table(_long_summary())
    assert max(len(line) for line in out.splitlines()) <= 80
    for label in ("t01-0951", "s03", "humanize", "found step 9", "found step 8", "3 tests"):
        assert label in out, label
    assert "unknown" not in out and "no result" not in out
    assert "…" in out  # the long repaired-break test name is cut with an ellipsis
    # The run table has one line per run and decisive step: 3 tests collapsed into one line.
    assert sum(line.split()[:1] == ["humanize"] for line in out.splitlines()) == 1


def test_study_table_is_the_same_at_other_widths_without_overflow() -> None:
    for width in (60, 100):
        out = _render_study_table(_long_summary(), width)
        assert max(len(line) for line in out.splitlines()) <= width
