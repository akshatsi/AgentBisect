"""Shadow-git snapshots.

Every snapshot is a commit in ``<run>/shadow.git``, taken with
``git --git-dir=<run>/shadow.git --work-tree=<dir>``. The workdir's own ``.git`` is never read or
written: it is excluded from snapshots, and the shadow repo ignores the user's git config.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 120.0

#: Paths never snapshotted. ``.git`` keeps the user's repo out; ``.opencode/`` holds the recorder plugin; the rest is noise that would make
#: identical states look different (bytecode, tool caches) or bloat the shadow repo.
EXCLUDES = (
    ".git",
    ".opencode/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".tox/",
    "build/",
    "dist/",
    "*.egg-info/",
    "htmlcov/",
    ".coverage",
    ".DS_Store",
)


def _pathspec(pattern: str) -> str:
    """``EXCLUDES`` entry as a git exclude pathspec that matches at any depth."""
    body = pattern.rstrip("/")
    tail = "/**" if pattern.endswith("/") or pattern == ".git" else ""
    return f":(exclude,glob)**/{body}{tail}"


#: Snapshots ignore the project's own ``.gitignore``: generated files such as a build-time
#: ``_version.py`` are often ignored there, and tests fail to import without them.
EXCLUDE_PATHSPECS = tuple(_pathspec(p) for p in EXCLUDES)


class SnapshotError(RuntimeError):
    pass


def shadow_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "shadow.git"


def _env() -> dict[str, str]:
    # Drop every inherited GIT_* variable (GIT_DIR, GIT_INDEX_FILE, ...) so we never act on the
    # caller's repo, and ignore global/system config so results do not depend on the machine.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="agentbisect",
        GIT_AUTHOR_EMAIL="agentbisect@localhost",
        GIT_COMMITTER_NAME="agentbisect",
        GIT_COMMITTER_EMAIL="agentbisect@localhost",
    )
    return env


def _git(
    run_dir: Path,
    *args: str,
    workdir: Path | None = None,
    input: bytes | None = None,
) -> bytes:
    cmd = ["git", f"--git-dir={shadow_dir(run_dir).resolve()}"]
    if workdir is not None:
        cmd.append(f"--work-tree={Path(workdir).resolve()}")
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            env=_env(),
            timeout=GIT_TIMEOUT_S,
            cwd=Path(workdir).resolve() if workdir is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise SnapshotError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {err}")
    return proc.stdout


def _git_text(run_dir: Path, *args: str, workdir: Path | None = None) -> str:
    return _git(run_dir, *args, workdir=workdir).decode(errors="replace").strip()


def init_shadow(run_dir: Path, workdir: Path) -> str:
    """Create ``<run_dir>/shadow.git`` and commit the workdir as the base (step 0). Returns its SHA."""
    run_dir, workdir = Path(run_dir), Path(workdir)
    if shadow_dir(run_dir).exists():
        raise FileExistsError(f"{shadow_dir(run_dir)} already exists")
    run_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "init", "--quiet", "--bare", str(shadow_dir(run_dir))],
        capture_output=True,
        env=_env(),
        timeout=GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise SnapshotError(f"git init failed: {proc.stderr.decode(errors='replace').strip()}")
    for key, value in (
        ("gc.auto", "0"),
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
    ):
        _git(run_dir, "config", key, value)
    info = shadow_dir(run_dir) / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("\n".join(EXCLUDES) + "\n")
    sha, _tree = _commit_tree(run_dir, workdir, "step 0: base", dedup=False)
    return sha


def _write_tree(run_dir: Path, workdir: Path) -> str:
    _git(run_dir, "add", "-A", "--force", "--", ".", *EXCLUDE_PATHSPECS, workdir=workdir)
    return _git_text(run_dir, "write-tree", workdir=workdir)


def _known_trees(run_dir: Path) -> dict[str, str]:
    """Map tree hash -> commit SHA for every commit in the shadow repo (first commit wins)."""
    out = _git_text(run_dir, "log", "--all", "--reverse", "--format=%H %T")
    known: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, tree = line.partition(" ")
        known.setdefault(tree, sha)
    return known


def tree_map(run_dir: Path) -> dict[str, str]:
    """Map every snapshot SHA in the shadow repo to its tree hash (one git call)."""
    out = _git_text(Path(run_dir), "log", "--all", "--format=%H %T")
    return {sha: tree for sha, _, tree in (line.partition(" ") for line in out.splitlines())}


def _commit_tree(run_dir: Path, workdir: Path, message: str, *, dedup: bool) -> tuple[str, str]:
    tree = _write_tree(run_dir, workdir)
    if dedup:
        existing = _known_trees(run_dir).get(tree)
        if existing is not None:
            return existing, tree
    parent: list[str] = []
    head = subprocess.run(
        [
            "git",
            f"--git-dir={shadow_dir(run_dir).resolve()}",
            "rev-parse",
            "--verify",
            "-q",
            "HEAD",
        ],
        capture_output=True,
        env=_env(),
        timeout=GIT_TIMEOUT_S,
    )
    if head.returncode == 0:
        parent = ["-p", head.stdout.decode().strip()]
    sha = _git_text(run_dir, "commit-tree", tree, *parent, "-m", message or "snapshot")
    _git(run_dir, "update-ref", "HEAD", sha)
    return sha, tree


def take_snapshot(run_dir: Path, workdir: Path, message: str) -> tuple[str, str]:
    """Snapshot the workdir. Returns ``(sha, tree)``.

    If the tree matches an earlier snapshot, no commit is made and that snapshot's SHA is returned,
    so distinct SHAs are distinct repo states.
    """
    return _commit_tree(Path(run_dir), Path(workdir), message, dedup=True)


def restore(run_dir: Path, sha: str, scratch_dir: Path) -> Path:
    """Replace the contents of ``scratch_dir`` with snapshot ``sha``. Never touches the workdir."""
    scratch_dir = Path(scratch_dir)
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True)
    archive = _git(Path(run_dir), "archive", "--format=tar", sha)
    proc = subprocess.run(
        ["tar", "-xf", "-", "-C", str(scratch_dir)],
        input=archive,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise SnapshotError(f"tar extract failed: {proc.stderr.decode(errors='replace').strip()}")
    return scratch_dir


def diff(run_dir: Path, sha_a: str, sha_b: str, max_chars: int = 200_000) -> str:
    """Unified diff from snapshot ``sha_a`` to ``sha_b``."""
    text = _git(
        Path(run_dir), "diff", "--no-color", "--no-ext-diff", "--no-renames", sha_a, sha_b
    ).decode(errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... diff truncated at {max_chars} characters\n"
    return text


def changed_files(run_dir: Path, sha_a: str, sha_b: str) -> list[str]:
    """Paths that differ between snapshots ``sha_a`` and ``sha_b``, sorted, repo-relative."""
    raw = _git(
        Path(run_dir), "diff", "--name-only", "-z", "--no-ext-diff", "--no-renames", sha_a, sha_b
    )
    return sorted(p for p in raw.decode(errors="replace").split("\0") if p)


def read_file(run_dir: Path, sha: str, path: str) -> str | None:
    """Contents of ``path`` in snapshot ``sha``, or None if absent."""
    try:
        return (
            io.BytesIO(_git(Path(run_dir), "show", f"{sha}:{path}")).read().decode(errors="replace")
        )
    except SnapshotError:
        return None
