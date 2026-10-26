"""The oracle runs in the project's own environment, on the snapshot's code."""

import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.bisect import bisect, bisect_run
from agentbisect.models import (
    BisectStatus,
    OracleConfig,
    Step,
    ToolKind,
    Trace,
    TraceMeta,
    TraceSource,
)
from agentbisect.oracle import check_env, execute_check

GOOD = "def ok():\n    return True\n"
BAD = "def ok():\n    return False\n"
CHECK = f"python -c {shlex.quote('import pkg; raise SystemExit(0 if pkg.ok() else 1)')}"


def make_venv(root: Path) -> Path:
    """A venv with no pip (fast). Returns its interpreter."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)
    return root / ("Scripts" if os.name == "nt" else "bin") / "python"


def install_non_editable(python: Path, source: str) -> None:
    """Copy package ``pkg`` into the venv's site-packages, as ``pip install .`` would."""
    out = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    )
    pkg = Path(out.stdout.strip()) / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(source)


def src_layout_trace(tmp_path: Path, break_at: int, n: int = 6) -> tuple[Trace, Path]:
    work, run = tmp_path / "work", tmp_path / "run"
    (work / "src" / "pkg").mkdir(parents=True)
    mod, readme = work / "src" / "pkg" / "__init__.py", work / "README"
    mod.write_text(GOOD)
    readme.write_text("0\n")
    base = snapshot.init_shadow(run, work)
    steps = []
    for i in range(1, n + 1):
        if i == break_at:
            mod.write_text(BAD)
        else:
            readme.write_text(f"{i}\n")
        sha, tree = snapshot.take_snapshot(run, work, f"step {i}")
        steps.append(
            Step(
                index=i,
                tool="edit_file",
                kind=ToolKind.EDIT,
                args={"path": "x"},
                snapshot=sha,
                tree=tree,
            )
        )
    meta = TraceMeta(
        run_id="r",
        source=TraceSource.SYNTHETIC,
        workdir=work,
        base_snapshot=base,
        created_at=datetime.now(UTC),
    )
    trace = Trace(meta=meta, steps=steps)
    trace.save(run)
    return trace, run


@pytest.mark.slow
def test_oracle_sees_the_snapshot_not_the_installed_package(tmp_path: Path) -> None:
    trace, run = src_layout_trace(tmp_path, break_at=4)
    python = make_venv(tmp_path / "venv")
    install_non_editable(python, BAD)  # site-packages holds the final (broken) version
    trace.meta.python = python
    result = bisect(trace, run, OracleConfig(check=CHECK, timeout_s=60))
    assert result.status is BisectStatus.FOUND, result.reason
    assert result.decisive_step == 4
    report = bisect_run(trace, run, CHECK, timeout_s=60)
    assert report.culprits[0].bisect.decisive_step == 4


def test_check_env_puts_the_project_python_first(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n")
    scratch = tmp_path / "scratch"
    (scratch / "src").mkdir(parents=True)
    os.environ.update({"PYTHONHOME": "/nope", "PYTHONPATH": "/keep", "VIRTUAL_ENV": "/other"})
    try:
        env = check_env(scratch, venv / "bin" / "python")
    finally:
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            os.environ.pop(key)
    assert env["PATH"].startswith(str(venv / "bin") + os.pathsep)
    assert env["VIRTUAL_ENV"] == str(venv)
    assert "PYTHONHOME" not in env
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts == [str(scratch), str(scratch / "src"), "/keep"]
    assert env["PYTEST_ADDOPTS"].startswith(f"--rootdir={shlex.quote(str(scratch))}")


def test_check_env_without_src_and_with_default_python(tmp_path: Path) -> None:
    env = check_env(tmp_path)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)
    assert str(tmp_path / "src") not in env["PYTHONPATH"]
    assert env["PATH"].startswith(str(Path(sys.executable).parent))
    out = execute_check("python -c 'import sys; print(sys.path[0:2])'", tmp_path, 30)
    assert out.exit_code == 0
