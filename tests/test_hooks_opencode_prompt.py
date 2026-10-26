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
