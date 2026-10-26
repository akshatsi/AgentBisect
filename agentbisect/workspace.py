"""Bring-your-own-repo workspace: copy or clone a project, and build its test environment.

``materialize`` puts the project files in ``runs/<id>/workdir`` without touching the original and
without running anything from it. ``setup_env`` then builds a venv *outside* the workdir
(``runs/<id>/venv``, so it is never snapshotted) and installs the project's dependencies.

SECURITY: ``setup_env`` installs the repo's dependencies, which can run the repo's build code
(``setup.py``, build backends). Only use repos you trust. ``materialize`` itself runs no repo code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Directory and file names that are never copied from a local project.
COPY_EXCLUDES = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".tox",
    "node_modules",
    ".opencode",
    "build",
    "dist",
    "*.egg-info",
)
#: Extras and dependency groups installed when the project defines them.
TEST_EXTRAS = ("test", "tests", "testing", "dev")
TEST_GROUPS = ("test", "tests", "dev")
GIT_SCHEMES = ("https://", "file://")
LOG_TAIL_CHARS = 6000


class SetupError(RuntimeError):
    """Environment setup failed. ``log_path`` is the ``setup.log`` with the command output."""

    def __init__(self, message: str, log_path: Path) -> None:
        super().__init__(f"{message} (see {log_path})")
        self.message = message
        self.log_path = log_path


# --------------------------------------------------------------------------- materialize


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="https:file",
    )
    return env


def _is_bare_repo(path: Path) -> bool:
    return (
        (path / "HEAD").is_file() and (path / "objects").is_dir() and not (path / ".git").exists()
    )


def _copy_ignore(origin: Path, dest: Path):
    """Ignore function: the standard excludes, plus any directory that holds ``dest`` itself
    (so running with ``runs/`` inside the project cannot copy the copy into itself)."""
    base = shutil.ignore_patterns(*COPY_EXCLUDES)
    dest_abs = dest.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = set(base(directory, names))
        for name in names:
            path = (Path(directory) / name).resolve()
            if path == dest_abs or path in dest_abs.parents:
                skip.add(name)
        return skip

    return ignore


def materialize(origin: str, dest: Path, *, ref: str | None = None, timeout_s: int = 300) -> None:
    """Put the project at ``origin`` into ``dest``. ``origin`` is a local directory or a git URL.

    A local directory is copied (symlinks stay symlinks, excludes in ``COPY_EXCLUDES``). A git URL
    (``https://`` or ``file://``) is cloned with ``--depth 1`` (``ref`` is a branch or tag), and the
    clone's ``.git`` is removed. The original is only read. Nothing from the repo runs.
    """
    dest = Path(dest)
    local = Path(origin).expanduser()
    if local.is_dir() and not _is_bare_repo(local):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            local, dest, symlinks=True, ignore=_copy_ignore(local, dest), dirs_exist_ok=True
        )
        return
    url = f"file://{local.resolve()}" if local.is_dir() else origin
    if not url.startswith(GIT_SCHEMES):
        raise ValueError(f"{origin!r} is not a directory and not an https:// or file:// git URL")
    if ref is not None and ref.startswith("-"):
        raise ValueError(f"bad ref {ref!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--quiet", "--depth", "1", "--no-tags"]
    if ref:
        cmd += ["--branch", ref]
    cmd += ["--", url, str(dest)]
    try:
        proc = subprocess.run(
            cmd, env=_git_env(), capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone of {origin} timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone of {origin} failed: {proc.stderr.strip()[-500:]}")
    shutil.rmtree(dest / ".git", ignore_errors=True)
    shutil.rmtree(dest / ".opencode", ignore_errors=True)


# --------------------------------------------------------------------------- setup_env


def _run(cmd: Sequence[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
    """Run one command. Returns (exit code, combined output). Never raises."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return 124, f"timed out after {timeout_s}s\n{out}"
    except OSError as exc:
        return 126, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def run(self, cmd: Sequence[str], cwd: Path, timeout_s: int) -> int:
        code, out = _run(cmd, cwd, timeout_s)
        tail = out[-LOG_TAIL_CHARS:]
        with self.path.open("a") as f:
            f.write(f"$ {' '.join(cmd)}\n[exit {code}]\n{tail}\n\n")
        return code

    def note(self, text: str) -> None:
        with self.path.open("a") as f:
            f.write(text.rstrip() + "\n\n")


def _load_pyproject(workdir: Path) -> dict[str, Any] | None:
    path = workdir / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return {}


def requirements_files(workdir: Path) -> list[Path]:
    """Requirements files to install: ``requirements*.txt`` (docs ones skipped) and
    ``requirements/test*.txt``, in sorted order."""
    found = [
        p
        for p in sorted(workdir.glob("requirements*.txt"))
        if p.is_file() and "doc" not in p.name.lower()
    ]
    found += [p for p in sorted(workdir.glob("requirements/test*.txt")) if p.is_file()]
    return found


def plan_install(workdir: Path, python: Path) -> list[list[str]]:
    """The ``uv pip install`` commands for this project layout, in order (pytest not included).

    * ``requirements*.txt`` and ``requirements/test*.txt``: one command with every file as ``-r``.
    * ``pyproject.toml`` with a ``[project]`` table: a normal (not editable) install of the
      project with whichever of the test/tests/testing/dev extras it defines, plus
      ``--group <pyproject>:<name>`` for each of the test/tests/dev dependency groups it defines.
    * a ``pyproject.toml`` with only dependency groups: just the ``--group`` flags.
    * ``setup.py`` / ``setup.cfg`` without a pyproject: a normal install of the directory.
    """
    base = ["uv", "pip", "install", "--python", str(python)]
    commands: list[list[str]] = []
    reqs = requirements_files(workdir)
    if reqs:
        cmd = list(base)
        for r in reqs:
            cmd += ["-r", str(r)]
        commands.append(cmd)
    pyproject = _load_pyproject(workdir)
    if pyproject is not None:
        extras = [
            e
            for e in TEST_EXTRAS
            if e in (pyproject.get("project", {}).get("optional-dependencies") or {})
        ]
        groups = [g for g in TEST_GROUPS if g in (pyproject.get("dependency-groups") or {})]
        target = str(workdir) + (f"[{','.join(extras)}]" if extras else "")
        cmd = list(base)
        if "project" in pyproject:
            cmd.append(target)
        for g in groups:
            cmd += ["--group", f"{workdir / 'pyproject.toml'}:{g}"]
        if len(cmd) > len(base):
            commands.append(cmd)
    elif (workdir / "setup.py").is_file() or (workdir / "setup.cfg").is_file():
        commands.append([*base, str(workdir)])
    return commands


def _fallback_dependency_command(workdir: Path, python: Path) -> list[str] | None:
    """Install only the declared dependency strings (no build of the project itself). Used when
    the project cannot be built, such as flat modules with no build backend."""
    pyproject = _load_pyproject(workdir) or {}
    project = pyproject.get("project") or {}
    deps: list[str] = list(project.get("dependencies") or [])
    optional = project.get("optional-dependencies") or {}
    for e in TEST_EXTRAS:
        deps += [d for d in optional.get(e, []) if isinstance(d, str)]
    for g in TEST_GROUPS:
        deps += [
            d for d in (pyproject.get("dependency-groups") or {}).get(g, []) if isinstance(d, str)
        ]
    deps = list(dict.fromkeys(deps))
    if not deps:
        return None
    return ["uv", "pip", "install", "--python", str(python), *deps]


def setup_env(workdir: Path, venv_dir: Path, *, timeout_s: int = 900) -> Path:
    """Build ``venv_dir`` and install the project's dependencies. Returns the venv's python.

    The log (every command, exit code and output tail) goes to ``<venv_dir>/../setup.log``.
    Raises ``SetupError`` on failure.
    """
    workdir, venv_dir = Path(workdir).resolve(), Path(venv_dir).resolve()
    log = _Log(venv_dir.parent / "setup.log")

    def fail(message: str) -> SetupError:
        log.note(f"FAILED: {message}")
        return SetupError(message, log.path)

    cwd = workdir
    if log.run(["uv", "venv", "--python", "3.11", str(venv_dir)], cwd, timeout_s) != 0:
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        log.note(f"python 3.11 unavailable; trying the current interpreter version {current}")
        shutil.rmtree(venv_dir, ignore_errors=True)
        if log.run(["uv", "venv", "--python", current, str(venv_dir)], cwd, timeout_s) != 0:
            raise fail("could not create the virtual environment with uv")
    python = venv_dir / "bin" / "python"
    if not python.exists():
        python = venv_dir / "Scripts" / "python.exe"
    if not python.exists():
        raise fail(f"uv venv made no python under {venv_dir}")

    for cmd in plan_install(workdir, python):
        if log.run(cmd, cwd, timeout_s) == 0:
            continue
        builds_project = any(Path(a.split("[")[0]) == workdir for a in cmd[5:])
        fallback = _fallback_dependency_command(workdir, python) if builds_project else None
        if fallback:
            log.note("project install failed; installing its declared dependencies only")
            if log.run(fallback, cwd, timeout_s) == 0:
                continue
        raise fail(f"dependency install failed: {' '.join(cmd[:4])} ...")
    if log.run([str(python), "-c", "import pytest"], cwd, 120) != 0:
        pytest_cmd = ["uv", "pip", "install", "--python", str(python), "pytest"]
        if log.run(pytest_cmd, cwd, timeout_s) != 0:
            raise fail("could not install pytest")
    return python


def detect_test_command(workdir: Path) -> str:
    """``python -m pytest -q`` plus ``tests`` or ``test`` if that directory exists."""
    for name in ("tests", "test"):
        if (Path(workdir) / name).is_dir():
            return f"python -m pytest -q {name}"
    return "python -m pytest -q"
