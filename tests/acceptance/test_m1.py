"""Acceptance checks for milestone M1 (snapshot + oracle + bisect + `agentbisect run`).

Black-box: every check drives the real CLI (`python -m agentbisect.cli run`) on traces built by
`agentbisect.synthetic`, then reads `bisect.json`. No network, no LLM. Run dirs live in pytest temp
dirs, except one test that puts a run dir under the project's own `runs/` and cleans it up.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.models import Step, ToolKind
from agentbisect.synthetic import flaky_check, make_synthetic_run

PROJECT = Path(__file__).resolve().parents[2]
N = 30
TEST_ADD = "python -m pytest -q tests/test_calc.py::test_add"
TEST_ANSWER = "python -m pytest -q tests/test_calc.py::test_answer"


# --------------------------------------------------------------------------- helpers


def cli_run(run_dir: Path, check: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "agentbisect.cli", "run", "--trace", str(run_dir), "--check", check],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=run_dir.parent,
    )


def bisect_json(run_dir: Path) -> dict:
    data = json.loads((run_dir / "bisect.json").read_text())
    return data["culprits"][0]["bisect"]


def search_probes(b: dict) -> int:
    """Probes beyond the two precondition probes (step 0 and the final step)."""
    return len(b["probes"]) - b["precondition_probes"]


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, timeout=60
    )
    return out.stdout + out.stderr


def hash_tree(root: Path, skip: tuple[str, ...] = ()) -> str:
    """Hash every file path + content under ``root`` (skipping top-level names in ``skip``)."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in skip or "__pycache__" in rel.parts:
            continue
        h.update(str(rel).encode())
        if path.is_file() and not path.is_symlink():
            h.update(path.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- criterion 1 and 2


@pytest.mark.parametrize("k", [1, 15, 30])
def test_c1_c2_finds_known_break_within_probe_bound(tmp_path: Path, k: int) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=k)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    b = bisect_json(run)
    assert b["status"] == "found"
    assert b["decisive_step"] == k
    assert b["non_monotone"] is False
    # Criterion 2: <= ceil(log2 n) search probes, plus the precondition probes.
    bound = math.ceil(math.log2(N))
    assert search_probes(b) <= bound, (search_probes(b), bound)
    assert len(b["probes"]) <= bound + 2
    # The report also names the right step in the terminal.
    assert f"Step {k}" in proc.stdout


# --------------------------------------------------------------------------- criterion 3


@pytest.mark.parametrize("seed", range(6))
def test_c3_flaky_oracle_p01_k15(tmp_path: Path, seed: int) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15)
    check = flaky_check(run, p=0.1, seed=seed)
    proc = cli_run(run, check)
    b = bisect_json(run)
    print(f"seed={seed} status={b['status']} step={b['decisive_step']} probes={len(b['probes'])}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b["status"] == "found"
    assert b["decisive_step"] == 15


@pytest.mark.parametrize("k", [1, 30])
@pytest.mark.parametrize("seed", [10, 11, 12])
def test_c3_flaky_oracle_p01_extreme_breaks(tmp_path: Path, k: int, seed: int) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=k)
    proc = cli_run(run, flaky_check(run, p=0.1, seed=seed))
    b = bisect_json(run)
    print(f"k={k} seed={seed} status={b['status']} step={b['decisive_step']}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b["decisive_step"] == k


# --------------------------------------------------------------------------- criterion 4


def _snapshot_repo_state(repo: Path) -> dict[str, str]:
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "log": git(repo, "log", "--format=%H %s"),
        "status": git(repo, "status", "--porcelain", "--ignored=no"),
        "branches": git(repo, "branch", "-a", "--format=%(refname) %(objectname)"),
    }


def test_c4_workdir_git_repo_is_untouched(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15, git_workdir=True)
    workdir = run / "workdir"
    assert (workdir / ".git").is_dir()
    before_git = hash_tree(workdir / ".git")
    before_files = hash_tree(workdir, skip=(".git",))
    before_state = _snapshot_repo_state(workdir)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["decisive_step"] == 15
    assert hash_tree(workdir / ".git") == before_git, "workdir .git changed"
    assert hash_tree(workdir, skip=(".git",)) == before_files, "workdir files changed"
    assert _snapshot_repo_state(workdir) == before_state
    # Snapshots live only in the shadow repo.
    assert (run / "shadow.git").is_dir()
    assert "agentbisect" not in git(workdir, "log", "--format=%an")


def test_c4_project_repo_is_untouched_with_run_dir_inside_it() -> None:
    """Run dir under the project's own (git-ignored) runs/ dir, so git sees a parent repo."""
    before = _snapshot_repo_state(PROJECT)
    git_dir_before = hash_tree(PROJECT / ".git", skip=("index", "logs", "COMMIT_EDITMSG"))
    run = PROJECT / "runs" / f"_acceptance_m1_{uuid.uuid4().hex[:8]}"
    try:
        make_synthetic_run(run, n_steps=N, break_at=15)
        proc = cli_run(run, TEST_ADD)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert bisect_json(run)["decisive_step"] == 15
        after = _snapshot_repo_state(PROJECT)
        assert after == before, f"project repo changed: {before} -> {after}"
        assert (
            hash_tree(PROJECT / ".git", skip=("index", "logs", "COMMIT_EDITMSG")) == git_dir_before
        )
    finally:
        shutil.rmtree(run, ignore_errors=True)


def test_c4_project_repo_untouched_when_workdir_is_git_repo_inside_it() -> None:
    before = _snapshot_repo_state(PROJECT)
    run = PROJECT / "runs" / f"_acceptance_m1_{uuid.uuid4().hex[:8]}"
    try:
        make_synthetic_run(run, n_steps=N, break_at=7, git_workdir=True)
        proc = cli_run(run, TEST_ADD)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert bisect_json(run)["decisive_step"] == 7
        assert _snapshot_repo_state(PROJECT) == before
    finally:
        shutil.rmtree(run, ignore_errors=True)


# --------------------------------------------------------------------------- edge cases


def test_edge_break_at_step_1_with_duplicate_snapshots(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=1, noop_every=2)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["decisive_step"] == 1


def test_edge_break_at_step_1_all_later_steps_are_duplicates(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=10, break_at=1, noop_every=1)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["decisive_step"] == 1


def test_edge_break_at_last_step_with_duplicates_before_it(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=10, break_at=10, noop_every=1)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["decisive_step"] == 10


def test_edge_break_in_the_middle_attributed_to_the_edit_not_a_duplicate(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=14, noop_every=3)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["decisive_step"] == 14


def test_edge_oracle_never_fails_is_not_bisectable_with_reason(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15)
    proc = cli_run(run, TEST_ANSWER)  # test_answer passes at every step
    b = bisect_json(run)
    assert b["status"] == "not_bisectable"
    assert b["decisive_step"] is None
    assert b["reason"] and "final step" in b["reason"]
    assert proc.returncode != 0
    assert "Not bisectable" in proc.stdout


def test_edge_oracle_already_failing_at_step_0_is_not_bisectable(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15)
    proc = cli_run(run, "python -c 'import sys; sys.exit(1)'")
    b = bisect_json(run)
    assert b["status"] == "not_bisectable"
    assert b["reason"] and "step 0" in b["reason"]
    assert proc.returncode != 0


def test_edge_history_that_ends_fixed_is_not_bisectable(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=10, fix_at=20)
    proc = cli_run(run, TEST_ADD)
    b = bisect_json(run)
    assert b["status"] == "not_bisectable"
    assert b["reason"] and "final step" in b["reason"]
    assert proc.returncode != 0


def _write_oracle_script(path: Path) -> Path:
    path.write_text(
        "import sys\n"
        "s = open('calc/__init__.py').read()\n"
        "sys.exit(1 if ('a - b' in s or 'note 24' in s) else 0)\n"
    )
    return path


def test_edge_non_monotone_without_repeated_tree_still_reports_last_break(tmp_path: Path) -> None:
    """Fails for steps 5..9, passes 10..23, fails again from 24.

    METHODS.md 3.2 says this is not detected by design (no tree repeats), so ``non_monotone`` stays
    False. The reported decisive step must still be the break the final state carries (24).
    """
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=5, fix_at=10)
    proc = cli_run(run, f"python {_write_oracle_script(tmp_path / 'oracle.py')}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    b = bisect_json(run)
    assert b["status"] == "found"
    assert b["decisive_step"] == 24
    assert b["non_monotone"] is False  # documented limitation, not a failure


def test_edge_non_monotone_break_fix_break_with_repeated_tree_lists_every_transition(
    tmp_path: Path,
) -> None:
    """Step 5 breaks, step 6 reverts it (tree == step 4), step 7 breaks again (tree == step 5)."""
    run = tmp_path / "r"
    trace = make_synthetic_run(run, n_steps=6, break_at=5, fix_at=6)
    workdir = run / "workdir"
    init = workdir / "calc" / "__init__.py"
    init.write_text(init.read_text().replace("return a + b", "return a - b"))
    sha, tree = snapshot.take_snapshot(run, workdir, "step 7: re-break")
    assert sha == trace.steps[4].snapshot, "re-break should reproduce the step 5 tree"
    trace.steps.append(
        Step(index=7, tool="edit_file", kind=ToolKind.EDIT, snapshot=sha, tree=tree, output="ok")
    )
    trace.save(run)
    proc = cli_run(run, TEST_ADD)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    b = bisect_json(run)
    assert b["status"] == "found"
    assert b["non_monotone"] is True
    assert b["transitions"] == [5, 7]
    assert b["decisive_step"] == 7


def test_edge_missing_check_command_is_not_bisectable_not_a_crash(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15)
    proc = cli_run(run, "definitely_not_a_real_command_xyz --flag")
    assert "Traceback" not in proc.stdout + proc.stderr
    b = bisect_json(run)
    assert b["status"] == "not_bisectable"
    assert b["decisive_step"] is None
    assert b["reason"]
    assert proc.returncode != 0


def test_edge_nonexistent_test_id_is_not_bisectable(tmp_path: Path) -> None:
    run = tmp_path / "r"
    make_synthetic_run(run, n_steps=N, break_at=15)
    proc = cli_run(run, "python -m pytest -q tests/test_calc.py::test_does_not_exist")
    assert "Traceback" not in proc.stdout + proc.stderr
    b = bisect_json(run)
    assert b["status"] == "not_bisectable"
    assert b["reason"]


def test_edge_missing_trace_dir_exits_cleanly(tmp_path: Path) -> None:
    env = dict(os.environ)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentbisect.cli",
            "run",
            "--trace",
            str(tmp_path / "nope"),
            "--check",
            "true",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stdout + proc.stderr
