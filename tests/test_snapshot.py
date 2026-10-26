import subprocess
from pathlib import Path

import pytest

from agentbisect import snapshot


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=u", "-c", "user.email=u@u", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir()
    (w / "a.py").write_text("a = 1\n")
    return w


def test_init_snapshot_dedup_and_restore(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    base = snapshot.init_shadow(run, work)
    (work / "a.py").write_text("a = 2\n")
    (work / "pkg").mkdir()
    (work / "pkg" / "b.py").write_text("b = 1\n")
    s1, tree1 = snapshot.take_snapshot(run, work, "edit")
    assert s1 != base
    again, tree_again = snapshot.take_snapshot(run, work, "nothing changed")
    assert (again, tree_again) == (s1, tree1)

    # Going back to an earlier state reuses the earlier snapshot.
    (work / "a.py").write_text("a = 1\n")
    (work / "pkg" / "b.py").unlink()
    (work / "pkg").rmdir()
    back, _ = snapshot.take_snapshot(run, work, "revert")
    assert back == base

    scratch = run / "scratch"
    snapshot.restore(run, s1, scratch)
    assert (scratch / "a.py").read_text() == "a = 2\n"
    assert (scratch / "pkg" / "b.py").read_text() == "b = 1\n"
    snapshot.restore(run, base, scratch)
    assert (scratch / "a.py").read_text() == "a = 1\n"
    assert not (scratch / "pkg").exists()


def test_diff(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    base = snapshot.init_shadow(run, work)
    (work / "a.py").write_text("a = 2\n")
    s1, _ = snapshot.take_snapshot(run, work, "edit")
    text = snapshot.diff(run, base, s1)
    assert "-a = 1" in text and "+a = 2" in text


def test_init_twice_fails(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    snapshot.init_shadow(run, work)
    with pytest.raises(FileExistsError):
        snapshot.init_shadow(run, work)


def test_noise_is_excluded(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    base = snapshot.init_shadow(run, work)
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"x")
    (work / ".pytest_cache").mkdir()
    (work / ".pytest_cache" / "v").write_text("1")
    sha, _ = snapshot.take_snapshot(run, work, "noise only")
    assert sha == base


def test_workdir_git_repo_is_untouched(tmp_path: Path, work: Path) -> None:
    _git(work, "init", "-q")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "user commit")
    log_before = _git(work, "log", "--format=%H")
    head_before = (work / ".git" / "HEAD").read_bytes()
    index_before = (work / ".git" / "index").read_bytes()
    refs_before = sorted(p.name for p in (work / ".git" / "refs" / "heads").iterdir())

    run = tmp_path / "run"
    snapshot.init_shadow(run, work)
    (work / "a.py").write_text("a = 3\n")
    sha, _ = snapshot.take_snapshot(run, work, "edit")
    snapshot.restore(run, sha, run / "scratch")

    assert _git(work, "log", "--format=%H") == log_before
    assert (work / ".git" / "HEAD").read_bytes() == head_before
    assert (work / ".git" / "index").read_bytes() == index_before
    assert sorted(p.name for p in (work / ".git" / "refs" / "heads").iterdir()) == refs_before
    # The user's repo sees the edit as an ordinary unstaged change, not as something we committed.
    assert _git(work, "status", "--porcelain").strip() == "M a.py"
    # And the snapshot holds no .git directory.
    assert not (run / "scratch" / ".git").exists()


def test_shadow_ignores_inherited_git_env(
    tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus-index"))
    run = tmp_path / "run"
    snapshot.init_shadow(run, work)
    snapshot.take_snapshot(run, work, "x")
    assert not (tmp_path / "bogus-index").exists()
    assert not list(other.glob("objects/??"))
