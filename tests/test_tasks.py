import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentbisect.llm import load_models
from agentbisect.models import TaskSpec

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
TASK_FILES = sorted([*TASKS.glob("t[0-9][0-9].yaml"), *TASKS.glob("s[0-9][0-9].yaml")])
SABOTAGE_FILES = [p for p in TASK_FILES if p.name.startswith("s")]


def load_task(path: Path) -> TaskSpec:
    return TaskSpec.model_validate(yaml.safe_load(path.read_text()))


def run_pytest(workdir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def copy_fixture(dest: Path) -> Path:
    shutil.copytree(
        ROOT / "tasks" / "fixture_repo",
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    return dest


def test_suite_has_ten_tasks_and_three_sabotage_tasks():
    assert [p.stem for p in TASK_FILES if p.stem.startswith("t")] == [
        f"t{i:02d}" for i in range(1, 11)
    ]
    assert [p.stem for p in SABOTAGE_FILES] == ["s01", "s02", "s03"]


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: p.stem)
def test_task_loads_and_points_at_the_fixture(path: Path):
    task = load_task(path)
    assert task.id == path.stem
    assert task.title and task.prompt.strip()
    assert (ROOT / task.repo).is_dir()
    assert (ROOT / task.repo / "tests").is_dir()
    assert 20 <= task.max_steps <= 30
    assert task.check is None  # auto-oracle
    assert (task.sabotage is not None) == path.stem.startswith("s")


def test_task_ids_are_unique():
    ids = [load_task(p).id for p in TASK_FILES]
    assert len(ids) == len(set(ids))


def test_fixture_has_no_git_dir_and_is_pure_python():
    repo = ROOT / "tasks" / "fixture_repo"
    assert not (repo / ".git").exists()
    assert len(list((repo / "shop").glob("*.py"))) >= 6
    imports = set()
    for f in repo.rglob("*.py"):
        imports |= set(re.findall(r"^(?:from|import) (\w+)", f.read_text(), re.M))
    stdlib_or_local = sys.stdlib_module_names | {"shop", "tests", "pytest"}
    assert imports <= stdlib_or_local, imports - stdlib_or_local


def test_fixture_suite_passes_in_a_temp_copy(tmp_path: Path):
    work = copy_fixture(tmp_path / "work")
    result = run_pytest(work)
    assert result.returncode == 0, result.stdout + result.stderr
    count = int(re.search(r"(\d+) passed", result.stdout).group(1))
    assert count >= 25


@pytest.mark.parametrize("path", SABOTAGE_FILES, ids=lambda p: p.stem)
def test_sabotage_find_string_exists_once_and_breaks_a_test(path: Path, tmp_path: Path):
    spec = load_task(path).sabotage
    assert spec is not None
    repo = ROOT / "tasks" / "fixture_repo"
    assert (repo / spec.file).read_text().count(spec.find) == 1
    assert spec.replace != spec.find

    work = copy_fixture(tmp_path / "work")
    target = work / spec.file
    target.write_text(target.read_text().replace(spec.find, spec.replace))
    result = run_pytest(work)
    assert result.returncode != 0, "sabotage did not break any test"
    assert re.search(r"\d+ failed", result.stdout)


def test_sabotage_steps_are_distinct():
    steps = [load_task(p).sabotage.at_step for p in SABOTAGE_FILES]
    assert steps == [3, 6, 9]


def test_original_fixture_is_not_modified_by_the_tests():
    # The sabotage tests work on temp copies; the shipped fixture must still match its text.
    for path in SABOTAGE_FILES:
        spec = load_task(path).sabotage
        assert spec.find in (ROOT / "tasks" / "fixture_repo" / spec.file).read_text()


def test_every_model_alias_in_models_yaml_has_the_expected_fields():
    raw = yaml.safe_load((TASKS / "models.yaml").read_text())["models"]
    keys = {"display_name", "role", "api_id", "price_in_per_1m", "price_out_per_1m", "tool_calls"}
    for alias, row in raw.items():
        assert set(row) == keys, alias
    assert set(load_models()) == set(raw)
