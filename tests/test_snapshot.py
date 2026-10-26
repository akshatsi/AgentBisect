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


def test_changed_files(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    base = snapshot.init_shadow(run, work)
    (work / "a.py").write_text("a = 2\n")
    (work / "pkg").mkdir()
    (work / "pkg" / "new file.py").write_text("b = 1\n")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "a.pyc").write_text("junk")
    s1, _ = snapshot.take_snapshot(run, work, "edit")
    assert snapshot.changed_files(run, base, s1) == ["a.py", "pkg/new file.py"]
    assert snapshot.changed_files(run, s1, s1) == []
    (work / "a.py").unlink()
    s2, _ = snapshot.take_snapshot(run, work, "delete")
    assert snapshot.changed_files(run, s1, s2) == ["a.py"]


def test_opencode_plugin_dir_is_never_snapshotted(tmp_path: Path, work: Path) -> None:
    run = tmp_path / "run"
    plugin = work / ".opencode" / "plugin"
    plugin.mkdir(parents=True)
    (plugin / "agentbisect.js").write_text("// recorder v1\n")
    base = snapshot.init_shadow(run, work)
    (plugin / "agentbisect.js").write_text("// recorder v2\n")
    (work / "a.py").write_text("a = 2\n")
    s1, _ = snapshot.take_snapshot(run, work, "edit")
    assert snapshot.changed_files(run, base, s1) == ["a.py"]
    assert ".opencode" not in snapshot.diff(run, base, s1)
    same, _ = snapshot.take_snapshot(run, work, "plugin only")
    (plugin / "other.js").write_text("x")
    still, _ = snapshot.take_snapshot(run, work, "plugin only")
    assert same == still == s1  # plugin changes alone make no new snapshot
    scratch = run / "scratch"
    snapshot.restore(run, s1, scratch)
    assert not (scratch / ".opencode").exists()


def test_snapshot_keeps_files_the_project_gitignores(tmp_path):
    """Generated files (e.g. a build-time _version.py) are often in .gitignore but needed by tests."""
    from agentbisect import snapshot

    wd = tmp_path / "wd"
    (wd / "pkg").mkdir(parents=True)
    (wd / ".gitignore").write_text("pkg/_version.py\nbuild/\n")
    (wd / "pkg" / "_version.py").write_text("v = '1'\n")
    (wd / "pkg" / "__pycache__").mkdir()
    (wd / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"0")
    (wd / "build").mkdir()
    (wd / "build" / "junk.txt").write_text("x")
    run = tmp_path / "run"
    base = snapshot.init_shadow(run, wd)
    files = snapshot._git_text(run, "ls-tree", "-r", "--name-only", base).split()
    assert "pkg/_version.py" in files
    assert not any("__pycache__" in f or f.startswith("build/") for f in files)
