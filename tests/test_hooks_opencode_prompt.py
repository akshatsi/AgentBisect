import shlex
import sys

from agentbisect.hooks.opencode import agent_prompt


def test_agent_prompt_pins_python_to_this_venv():
    out = agent_prompt("Run the tests with `python -m pytest`. Done.")
    assert f"`{shlex.quote(sys.executable)} -m pytest`" in out
    assert out.endswith("\n")


def test_workdir_gets_its_own_git_repo(tmp_path):
    import subprocess

    from agentbisect.hooks.opencode import prepare_opencode_run
    from agentbisect.models import TaskSpec

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    task = TaskSpec(id="t", title="t", prompt="p", repo=repo)
    run_dir = prepare_opencode_run(task, tmp_path / "runs", "flash", "r1")
    workdir = run_dir / "workdir"
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=workdir, capture_output=True, text=True
    )
    assert top.stdout.strip() == str(workdir.resolve())
    files = subprocess.run(
        ["git", "--git-dir", str(run_dir / "shadow.git"), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert files == ["a.py"]


def test_resumed_conversation_is_flagged(tmp_path):
    from agentbisect.hooks.opencode import _resumed

    assert not _resumed(tmp_path)
    (tmp_path / "events.jsonl").write_text('{"text": "<resume_context>\\nUser: old task"}\n')
    assert _resumed(tmp_path)


def test_task_run_records_this_python(tmp_path):
    from agentbisect.hooks.opencode import prepare_opencode_run
    from agentbisect.models import TaskSpec, Trace

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    task = TaskSpec(id="t", title="t", prompt="p", repo=repo)
    run_dir = prepare_opencode_run(task, tmp_path / "runs", "flash", "r1")
    assert Trace.load(run_dir).meta.python == __import__("pathlib").Path(sys.executable)


def test_repo_run_plants_sabotage_and_checks_find_text(tmp_path):
    import json

    import pytest

    from agentbisect.hooks.opencode import prepare_repo_run
    from agentbisect.models import SabotageSpec

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text("def add(a, b):\n    return a + b\n")
    good = SabotageSpec(at_step=2, file="m.py", find="a + b", replace="a - b")
    run = prepare_repo_run(
        str(repo), "p", tmp_path / "runs", run_id="r", setup=False, sabotage=good
    )
    assert json.loads((run / "task.json").read_text())["sabotage"]["at_step"] == 2
    bad = SabotageSpec(at_step=2, file="m.py", find="nope", replace="x")
    with pytest.raises(ValueError, match="exactly once"):
        prepare_repo_run(str(repo), "p", tmp_path / "runs", run_id="r2", setup=False, sabotage=bad)
    assert not (tmp_path / "runs" / "r2").exists()
