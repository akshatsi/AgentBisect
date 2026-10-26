"""Bring-your-own-repo tests. Offline: local dirs and file:// git repos only; `uv` is mocked."""

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentbisect import snapshot, workspace
from agentbisect.hooks.opencode import prepare_repo_run
from agentbisect.models import Trace

# --------------------------------------------------------------------------- helpers


def make_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    for junk in (".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".tox", "node_modules"):
        (root / junk).mkdir()
        (root / junk / "junk.txt").write_text("x")
    for junk in ("build", "dist", "pkg.egg-info", ".opencode"):
        (root / junk).mkdir()
        (root / junk / "junk.txt").write_text("x")
    (root / "tests" / "__pycache__").mkdir()
    (root / "tests" / "__pycache__" / "x.pyc").write_text("x")
    return root


def fingerprint(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return out


def names(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def git(*args: str, cwd: Path) -> None:
    env = {**snapshot._env()}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def make_bare_origin(tmp_path: Path) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    git("init", "-q", "-b", "main", cwd=src)
    git("add", "-A", cwd=src)
    git("commit", "-q", "-m", "first", cwd=src)
    git("tag", "v1", cwd=src)
    (src / "app.py").write_text("x = 2\n")
    git("commit", "-q", "-am", "second", cwd=src)
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return f"file://{bare}"


# --------------------------------------------------------------------------- materialize


def test_materialize_local_dir_copies_and_excludes(tmp_path):
    origin = make_project(tmp_path / "proj")
    before = fingerprint(origin)
    dest = tmp_path / "out" / "workdir"
    workspace.materialize(str(origin), dest)
    assert names(dest) == {"app.py", "tests", "tests/test_app.py"}
    assert fingerprint(origin) == before


def test_materialize_keeps_symlinks_as_links(tmp_path):
    origin = make_project(tmp_path / "proj")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (origin / "link.txt").symlink_to(outside)
    dest = tmp_path / "workdir"
    workspace.materialize(str(origin), dest)
    assert (dest / "link.txt").is_symlink()


def test_materialize_skips_dest_inside_origin(tmp_path):
    origin = make_project(tmp_path / "proj")
    dest = origin / "runs" / "r1" / "workdir"
    workspace.materialize(str(origin), dest)
    assert names(dest) == {"app.py", "tests", "tests/test_app.py"}


def test_materialize_file_url(tmp_path):
    url = make_bare_origin(tmp_path)
    dest = tmp_path / "workdir"
    workspace.materialize(url, dest)
    assert (dest / "app.py").read_text() == "x = 2\n"
    assert not (dest / ".git").exists()


def test_materialize_file_url_ref(tmp_path):
    url = make_bare_origin(tmp_path)
    dest = tmp_path / "workdir"
    workspace.materialize(url, dest, ref="v1")
    assert (dest / "app.py").read_text() == "x = 1\n"
    assert not (dest / ".git").exists()


def test_materialize_bare_repo_directory_is_cloned(tmp_path):
    url = make_bare_origin(tmp_path)
    dest = tmp_path / "workdir"
    workspace.materialize(url.removeprefix("file://"), dest)
    assert (dest / "app.py").exists()
    assert not (dest / "HEAD").exists()


@pytest.mark.parametrize("origin", ["git@github.com:a/b.git", "ext::sh -c id", "ftp://x/y", "nope"])
def test_materialize_rejects_other_origins(tmp_path, origin):
    with pytest.raises(ValueError):
        workspace.materialize(origin, tmp_path / "w")
    assert not (tmp_path / "w").exists()


def test_materialize_bad_ref_and_failed_clone_leave_nothing(tmp_path):
    url = make_bare_origin(tmp_path)
    with pytest.raises(ValueError):
        workspace.materialize(url, tmp_path / "w", ref="--upload-pack=x")
    with pytest.raises(RuntimeError, match="clone"):
        workspace.materialize(url, tmp_path / "w", ref="no-such-branch")
    assert not (tmp_path / "w").exists()


def test_detect_test_command(tmp_path):
    (tmp_path / "a").mkdir()
    assert workspace.detect_test_command(tmp_path / "a") == "python -m pytest -q"
    (tmp_path / "a" / "test").mkdir()
    assert workspace.detect_test_command(tmp_path / "a") == "python -m pytest -q test"
    (tmp_path / "a" / "tests").mkdir()
    assert workspace.detect_test_command(tmp_path / "a") == "python -m pytest -q tests"


# --------------------------------------------------------------------------- setup_env (mocked uv)


class FakeUv:
    """Replaces ``workspace._run``. Records commands; ``fail`` is a predicate on the command."""

    def __init__(self, fail=lambda cmd: False, pytest_present=True):
        self.calls: list[list[str]] = []
        self.fail = fail
        self.pytest_present = pytest_present

    def __call__(self, cmd, cwd, timeout_s):
        cmd = list(cmd)
        self.calls.append(cmd)
        if self.fail(cmd):
            return 1, "boom: no such package"
        if cmd[:2] == ["uv", "venv"]:
            py = Path(cmd[-1]) / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("")
            return 0, "created"
        if cmd[1:] and cmd[1] == "-c":
            return (0 if self.pytest_present else 1), ""
        return 0, "ok"

    def installs(self):
        return [c for c in self.calls if c[:3] == ["uv", "pip", "install"]]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "run" / "workdir"
    root.mkdir(parents=True)
    return root


def run_setup(monkeypatch, project, fake):
    monkeypatch.setattr(workspace, "_run", fake)
    return workspace.setup_env(project, project.parent / "venv")


def test_setup_requirements_files(monkeypatch, project):
    (project / "requirements.txt").write_text("flask\n")
    (project / "requirements-dev.txt").write_text("pytest\n")
    (project / "requirements-docs.txt").write_text("sphinx\n")
    (project / "requirements").mkdir()
    (project / "requirements" / "test.txt").write_text("pytest\n")
    (project / "requirements" / "prod.txt").write_text("gunicorn\n")
    fake = FakeUv()
    py = run_setup(monkeypatch, project, fake)
    venv = (project.parent / "venv").resolve()
    assert py == venv / "bin" / "python"
    assert fake.calls[0] == ["uv", "venv", "--python", "3.11", str(venv)]
    assert fake.installs() == [
        [
            "uv", "pip", "install", "--python", str(py),
            "-r", str(project.resolve() / "requirements-dev.txt"),
            "-r", str(project.resolve() / "requirements.txt"),
            "-r", str(project.resolve() / "requirements" / "test.txt"),
        ]
    ]  # fmt: skip
    log = (project.parent / "setup.log").read_text()
    assert "$ uv venv --python 3.11" in log and "[exit 0]" in log


def test_setup_pyproject_extras_and_groups(monkeypatch, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\ndependencies = ["requests"]\n'
        '[project.optional-dependencies]\ndev = ["ruff"]\ntest = ["pytest"]\ndocs = ["sphinx"]\n'
        '[dependency-groups]\ntest = ["pytest"]\nlint = ["ruff"]\n'
    )
    fake = FakeUv()
    py = run_setup(monkeypatch, project, fake)
    wd = project.resolve()
    assert fake.installs() == [
        [
            "uv", "pip", "install", "--python", str(py),
            "-e", f"{wd}[test,dev]",
            "--group", f"{wd / 'pyproject.toml'}:test",
        ]
    ]  # fmt: skip


def test_setup_pyproject_without_extras_installs_plain_project(monkeypatch, project):
    (project / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    fake = FakeUv()
    py = run_setup(monkeypatch, project, fake)
    assert fake.installs() == [
        ["uv", "pip", "install", "--python", str(py), "-e", str(project.resolve())]
    ]


def test_setup_dependency_groups_only(monkeypatch, project):
    (project / "pyproject.toml").write_text(
        '[tool.x]\na = 1\n[dependency-groups]\ndev = ["pytest"]\ntest = ["pytest-cov"]\n'
    )
    fake = FakeUv()
    py = run_setup(monkeypatch, project, fake)
    pp = project.resolve() / "pyproject.toml"
    assert fake.installs() == [
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(py),
            "--group",
            f"{pp}:test",
            "--group",
            f"{pp}:dev",
        ]
    ]


def test_setup_pyproject_with_nothing_to_install_still_ensures_pytest(monkeypatch, project):
    (project / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    fake = FakeUv(pytest_present=False)
    py = run_setup(monkeypatch, project, fake)
    assert fake.installs() == [["uv", "pip", "install", "--python", str(py), "pytest"]]


def test_setup_pytest_already_installed_is_not_reinstalled(monkeypatch, project):
    (project / "requirements.txt").write_text("pytest\n")
    fake = FakeUv(pytest_present=True)
    run_setup(monkeypatch, project, fake)
    assert len(fake.installs()) == 1


def test_setup_falls_back_to_current_python_version(monkeypatch, project):
    seen: list[list[str]] = []

    def first_venv_fails(cmd):
        if cmd[:2] == ["uv", "venv"]:
            seen.append(cmd)
            return len(seen) == 1
        return False

    fake = FakeUv(fail=first_venv_fails)
    py = run_setup(monkeypatch, project, fake)
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert [c[3] for c in seen] == ["3.11", current]
    assert py.exists()


def test_setup_project_build_failure_falls_back_to_declared_dependencies(monkeypatch, project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\ndependencies = ["requests>=2"]\n'
        '[project.optional-dependencies]\ntest = ["pytest"]\n'
    )
    wd = str(project.resolve())
    fake = FakeUv(fail=lambda c: c[:3] == ["uv", "pip", "install"] and f"{wd}[test]" in c)
    py = run_setup(monkeypatch, project, fake)
    assert fake.installs()[-1] == [
        "uv",
        "pip",
        "install",
        "--python",
        str(py),
        "requests>=2",
        "pytest",
    ]
    assert "installing its declared dependencies only" in (project.parent / "setup.log").read_text()


def test_setup_error_has_log(monkeypatch, project):
    (project / "requirements.txt").write_text("no-such-package\n")
    fake = FakeUv(fail=lambda c: c[:3] == ["uv", "pip", "install"])
    monkeypatch.setattr(workspace, "_run", fake)
    with pytest.raises(workspace.SetupError) as err:
        workspace.setup_env(project, project.parent / "venv")
    assert err.value.log_path == project.parent / "setup.log"
    assert "dependency install failed" in str(err.value)
    text = err.value.log_path.read_text()
    assert "boom: no such package" in text and "[exit 1]" in text and "FAILED" in text


def test_setup_error_when_uv_missing(monkeypatch, project):
    monkeypatch.setenv("PATH", str(project))  # no uv here
    with pytest.raises(workspace.SetupError) as err:
        workspace.setup_env(project, project.parent / "venv")
    assert "command not found" in err.value.log_path.read_text()


def test_setup_real_offline(monkeypatch, project):
    """Real uv against its local cache only. Skips if the cache lacks pytest."""
    monkeypatch.setenv("UV_OFFLINE", "1")
    (project / "requirements.txt").write_text("pytest\n")
    try:
        py = workspace.setup_env(project, project.parent / "venv", timeout_s=120)
    except workspace.SetupError:
        pytest.skip("uv cache cannot satisfy pytest offline")
    out = subprocess.run([str(py), "-c", "import pytest"], capture_output=True)
    assert out.returncode == 0
    assert py.parent.parent == (project.parent / "venv").resolve()


# --------------------------------------------------------------------------- prepare_repo_run


def test_prepare_repo_run_local_dir(tmp_path):
    origin = make_project(tmp_path / "proj")
    before = fingerprint(origin)
    run_dir = prepare_repo_run(
        str(origin), "Make add faster.", tmp_path / "runs", model_alias="flash", setup=False
    )
    assert fingerprint(origin) == before
    assert run_dir.name.startswith("proj-flash-")
    workdir = run_dir / "workdir"
    assert (workdir / "app.py").exists() and not (workdir / "venv").exists()
    assert (workdir / ".opencode" / "plugin" / "agentbisect.js").exists()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=workdir, capture_output=True, text=True
    ).stdout.strip()
    assert top == str(workdir.resolve())
    trace = Trace.load(run_dir)
    assert trace.meta.task_id is None
    assert trace.meta.repo_origin == str(origin)
    assert trace.meta.python == Path(sys.executable)
    prompt = (run_dir / "PROMPT.txt").read_text()
    py = shlex.quote(sys.executable)
    assert prompt.startswith("Make add faster.")
    assert f"Run the tests with `{py} -m pytest -q tests`." in prompt
    assert "no `python` or `pytest`" in prompt


def test_prepare_repo_run_file_url_with_setup_uses_venv_python(monkeypatch, tmp_path):
    url = make_bare_origin(tmp_path)
    monkeypatch.setattr(workspace, "_run", FakeUv())
    run_dir = prepare_repo_run(url, "Fix it.", tmp_path / "runs", run_id="r1", ref="v1")
    assert run_dir.name == "r1"
    trace = Trace.load(run_dir)
    venv_py = run_dir / "venv" / "bin" / "python"
    assert trace.meta.python == venv_py
    assert trace.meta.repo_origin == url
    assert (run_dir / "workdir" / "app.py").read_text() == "x = 1\n"
    assert not (run_dir / "venv").resolve().is_relative_to((run_dir / "workdir").resolve())
    assert (
        f"`{shlex.quote(str(venv_py))} -m pytest -q tests`" in (run_dir / "PROMPT.txt").read_text()
    )
    assert (run_dir / "setup.log").exists()
    # the plugin calls back into AgentBisect, so it keeps this interpreter, not the project venv
    plugin = (run_dir / "workdir" / ".opencode" / "plugin" / "agentbisect.js").read_text()
    assert sys.executable in plugin and str(venv_py) not in plugin
    # venv is outside the workdir, so the shadow repo never sees it
    files = subprocess.run(
        ["git", "--git-dir", str(run_dir / "shadow.git"), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(files) == ["app.py", "tests/test_a.py"]


def test_prepare_repo_run_pins_python_in_user_prompt(tmp_path):
    origin = make_project(tmp_path / "proj")
    run_dir = prepare_repo_run(
        str(origin), "Run `python -m pytest` after.", tmp_path / "runs", setup=False
    )
    prompt = (run_dir / "PROMPT.txt").read_text()
    assert f"`{shlex.quote(sys.executable)} -m pytest` after." in prompt


def test_prepare_repo_run_setup_failure_keeps_run_dir_and_log(monkeypatch, tmp_path):
    origin = make_project(tmp_path / "proj")
    (origin / "requirements.txt").write_text("nope\n")
    monkeypatch.setattr(workspace, "_run", FakeUv(fail=lambda c: c[:3] == ["uv", "pip", "install"]))
    with pytest.raises(workspace.SetupError) as err:
        prepare_repo_run(str(origin), "p", tmp_path / "runs", run_id="r1")
    assert err.value.log_path == (tmp_path / "runs" / "r1").resolve() / "setup.log"
    assert err.value.log_path.exists()


def test_prepare_repo_run_bad_origin_cleans_up_and_refuses_existing_id(tmp_path):
    with pytest.raises(ValueError):
        prepare_repo_run("git@host:a/b.git", "p", tmp_path / "runs", run_id="r1", setup=False)
    assert not (tmp_path / "runs" / "r1").exists()
    origin = make_project(tmp_path / "proj")
    prepare_repo_run(str(origin), "p", tmp_path / "runs", run_id="r2", setup=False)
    with pytest.raises(FileExistsError):
        prepare_repo_run(str(origin), "p", tmp_path / "runs", run_id="r2", setup=False)


def test_prepare_repo_run_url_run_id_uses_repo_name(monkeypatch, tmp_path):
    url = make_bare_origin(tmp_path)
    run_dir = prepare_repo_run(url, "p", tmp_path / "runs", model_alias="flash", setup=False)
    assert run_dir.name.startswith("origin-flash-")
    assert os.path.isdir(run_dir / "workdir")


def test_local_env_files_are_not_copied(tmp_path):
    from agentbisect.workspace import materialize

    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")
    (src / ".env").write_text("SECRET=1\n")
    (src / ".env.local").write_text("SECRET=2\n")
    (src / ".env.example").write_text("SECRET=\n")
    dest = tmp_path / "copy"
    materialize(str(src), dest)
    assert sorted(p.name for p in dest.iterdir()) == [".env.example", "a.py"]
