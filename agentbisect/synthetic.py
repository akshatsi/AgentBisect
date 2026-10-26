"""Synthetic traces for tests and for the verifier. No LLM, no network.

Build one from the shell::

    python -m agentbisect.synthetic make --out runs/syn1 --steps 30 --break-at 15
    agentbisect run --trace runs/syn1 --check "<printed check>"

A tiny package ``calc`` and a pytest file live in a work directory next to the run. Every step
edits or adds a harmless file, except step ``break_at``, which makes ``test_add`` fail (and step
``fix_at``, if given, repairs it, so the history is non-monotone).

``python -m agentbisect.synthetic flaky`` wraps a command and flips its exit code (0 <-> 1) with
probability ``p``, using a seeded RNG whose state is a call counter kept in a file, so a flaky
oracle is reproducible.
"""

from __future__ import annotations

import argparse
import os
import random
import shlex
import subprocess
import sys
from pathlib import Path

from agentbisect import snapshot
from agentbisect.models import Step, ToolKind, Trace, TraceMeta, TraceSource

PYTEST_INI = "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -p no:cacheprovider\n"

TEST_FILE = """from calc import add, answer


def test_add():
    assert add(2, 3) == 5


def test_answer():
    assert answer() == 42
"""

INIT_HEADER = '''"""Tiny package under test."""


def answer():
    return 42


'''

GOOD_BODY = "def add(a, b):\n    return a + b\n"
BAD_BODY = "def add(a, b):\n    return a - b\n"

TOKENS_IN, TOKENS_OUT = 100, 50


def _init_source(body: str, comments: list[str]) -> str:
    return INIT_HEADER + body + "".join(f"\n# {c}" for c in comments) + "\n"


def flaky_check(run_dir: Path, p: float, seed: int, inner: str = "python -m pytest -q") -> str:
    """The check command that wraps ``inner`` in the seeded flaky wrapper."""
    state = Path(run_dir).resolve() / "flaky.counter"
    return (
        f"python -m agentbisect.synthetic flaky --p {p} --seed {seed} "
        f"--state {shlex.quote(str(state))} -- {inner}"
    )


def make_synthetic_run(
    run_dir: Path,
    n_steps: int = 30,
    break_at: int = 15,
    fix_at: int | None = None,
    flaky_p: float = 0.0,
    seed: int = 0,
    noop_every: int = 0,
    git_workdir: bool = False,
) -> Trace:
    """Create ``run_dir`` with a workdir, a shadow repo, and a saved trace of ``n_steps`` steps.

    ``noop_every=k`` makes every k-th step (other than ``break_at``/``fix_at``) a shell no-op, so
    its snapshot is deduplicated. ``git_workdir`` makes the workdir a git repo with one commit, to
    show that its ``.git`` is never touched. The suggested check goes to ``run_dir/check.txt``.
    """
    run_dir = Path(run_dir)
    if not 1 <= break_at <= n_steps:
        raise ValueError("break_at must be within 1..n_steps")
    if fix_at is not None and not break_at < fix_at <= n_steps:
        raise ValueError("fix_at must be within break_at+1..n_steps")
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = run_dir / "workdir"
    (workdir / "calc").mkdir(parents=True)
    (workdir / "tests").mkdir()
    (workdir / "pytest.ini").write_text(PYTEST_INI)
    (workdir / "tests" / "test_calc.py").write_text(TEST_FILE)

    body = GOOD_BODY
    comments: list[str] = []
    init_path = workdir / "calc" / "__init__.py"
    init_path.write_text(_init_source(body, comments))
    if git_workdir:
        for cmd in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-q", "-m", "user"],
        ):
            subprocess.run(cmd, cwd=workdir, check=True, capture_output=True, timeout=60)

    base = snapshot.init_shadow(run_dir, workdir)
    steps: list[Step] = []
    for i in range(1, n_steps + 1):
        special = i in (break_at, fix_at)
        reasoning = f"Step {i}."
        if i == break_at:
            old, body = body, BAD_BODY
            tool, kind = "edit_file", ToolKind.EDIT
            args = {"path": "calc/__init__.py", "old": old, "new": body}
            reasoning = "Simplify add()."
            init_path.write_text(_init_source(body, comments))
        elif i == fix_at:
            old, body = body, GOOD_BODY
            tool, kind = "edit_file", ToolKind.EDIT
            args = {"path": "calc/__init__.py", "old": old, "new": body}
            reasoning = "Restore add()."
            init_path.write_text(_init_source(body, comments))
        elif noop_every and i % noop_every == 0 and not special:
            tool, kind = "run_shell", ToolKind.SHELL
            args = {"command": "ls"}
        elif i % 2 == 0:
            tool, kind = "edit_file", ToolKind.EDIT
            comments.append(f"note {i}")
            args = {"path": "calc/__init__.py", "old": "", "new": f"# note {i}"}
            init_path.write_text(_init_source(body, comments))
        else:
            tool, kind = "write_file", ToolKind.WRITE
            rel = f"calc/gen/mod_{i:03d}.py"
            args = {"path": rel, "content": f"VALUE = {i}\n"}
            (workdir / "calc" / "gen").mkdir(exist_ok=True)
            (workdir / rel).write_text(f"VALUE = {i}\n")
        sha, tree = snapshot.take_snapshot(run_dir, workdir, f"step {i}: {tool}")
        steps.append(
            Step(
                index=i,
                tool=tool,
                kind=kind,
                args=args,
                output="ok",
                exit_code=0 if kind is ToolKind.SHELL else None,
                reasoning=reasoning,
                snapshot=sha,
                tree=tree,
                tokens_in=TOKENS_IN,
                tokens_out=TOKENS_OUT,
            )
        )

    meta = TraceMeta(
        run_id=run_dir.name,
        source=TraceSource.SYNTHETIC,
        workdir=workdir,
        base_snapshot=base,
        task_id="synthetic",
        sabotage_step=break_at,
        finished=True,
    )
    trace = Trace(meta=meta, steps=steps)
    trace.save(run_dir)
    check = (
        flaky_check(run_dir, flaky_p, seed)
        if flaky_p > 0
        else "python -m pytest -q tests/test_calc.py::test_add"
    )
    (run_dir / "check.txt").write_text(check + "\n")
    return trace


# --------------------------------------------------------------------------- flaky wrapper


def flaky_exit_code(real: int, p: float, seed: int, state: Path) -> tuple[int, bool]:
    """Advance the call counter in ``state`` and maybe flip ``real`` (0 <-> 1). Returns the code."""
    try:
        counter = int(state.read_text().strip())
    except (FileNotFoundError, ValueError):
        counter = 0
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(str(counter + 1))
    flip = random.Random(f"{seed}:{counter}").random() < p
    if flip and real in (0, 1):
        return 1 - real, True
    return real, False


def _cmd_flaky(args: argparse.Namespace) -> int:
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("flaky: no command given after --", file=sys.stderr)
        return 2
    state = Path(args.state or os.environ.get("AGENTBISECT_FLAKY_STATE", "flaky.counter"))
    proc = subprocess.run(cmd)
    code, flipped = flaky_exit_code(proc.returncode, args.p, args.seed, state)
    if flipped:
        print(f"[flaky] exit code flipped {proc.returncode} -> {code}", file=sys.stderr)
    return code


def _cmd_make(args: argparse.Namespace) -> int:
    out = Path(args.out)
    make_synthetic_run(
        out,
        n_steps=args.steps,
        break_at=args.break_at,
        fix_at=args.fix_at,
        flaky_p=args.flaky_p,
        seed=args.seed,
        noop_every=args.noop_every,
        git_workdir=args.git_workdir,
    )
    check = (out / "check.txt").read_text().strip()
    print(f"Built {args.steps}-step synthetic run in {out} (break at step {args.break_at}).")
    print("Run it with:")
    print(f"  agentbisect run --trace {shlex.quote(str(out))} --check {shlex.quote(check)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentbisect.synthetic")
    sub = parser.add_subparsers(dest="cmd", required=True)

    make = sub.add_parser("make", help="build a synthetic run directory")
    make.add_argument("--out", required=True)
    make.add_argument("--steps", type=int, default=30)
    make.add_argument("--break-at", type=int, required=True)
    make.add_argument("--fix-at", type=int, default=None)
    make.add_argument(
        "--flaky-p", type=float, default=0.0, help="suggest a flaky check with this p"
    )
    make.add_argument("--seed", type=int, default=0)
    make.add_argument("--noop-every", type=int, default=0)
    make.add_argument("--git-workdir", action="store_true", help="make the workdir a git repo")
    make.set_defaults(func=_cmd_make)

    flaky = sub.add_parser("flaky", help="run a command, flipping its exit code with probability p")
    flaky.add_argument("--p", type=float, required=True)
    flaky.add_argument("--seed", type=int, default=0)
    flaky.add_argument(
        "--state", default=None, help="counter file (or env AGENTBISECT_FLAKY_STATE)"
    )
    flaky.add_argument("command", nargs=argparse.REMAINDER)
    flaky.set_defaults(func=_cmd_flaky)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
