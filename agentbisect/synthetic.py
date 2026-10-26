"""Synthetic traces for tests and for the verifier. No LLM, no network.

Build one from the shell::

    python -m agentbisect.synthetic make --out runs/syn1 --steps 30 --break-at 15
    agentbisect run --trace runs/syn1 --check "<printed check>"

A tiny package ``calc`` and a pytest file live in a work directory next to the run. Every step
edits or adds a harmless file, except step ``break_at``, which makes ``test_add`` fail (and step
``fix_at``, if given, repairs it, so the history is non-monotone). ``break_again_at`` breaks it a
second time with a different bug, so the history is non-monotone with no repeated tree. With
``observe_at`` the step at that index is a shell step whose output shows the pytest failure
(exit code 1), so detection lag can be tested end to end.

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
BAD_BODY_2 = "def add(a, b):\n    return a * b\n"  # second, different break
_BAD_VALUE = {BAD_BODY: -1, BAD_BODY_2: 6}  # what add(2, 3) returns under each bad body
OBSERVE_CMD = "python -m pytest -q tests/test_calc.py::test_add"

TOKENS_IN, TOKENS_OUT = 100, 50


def _init_source(body: str, comments: list[str]) -> str:
    return INIT_HEADER + body + "".join(f"\n# {c}" for c in comments) + "\n"


def pytest_failure_output(value: int) -> str:
    """What ``pytest -q`` prints when ``test_add`` fails because ``add(2, 3)`` returned ``value``."""
    rule = "_" * 34
    return (
        "F" + " " * 71 + "[100%]\n"
        "=================================== FAILURES ===================================\n"
        f"{rule} test_add {rule}\n"
        "\n"
        "    def test_add():\n"
        ">       assert add(2, 3) == 5\n"
        f"E       assert {value} == 5\n"
        f"E        +  where {value} = add(2, 3)\n"
        "\n"
        "tests/test_calc.py:5: AssertionError\n"
        "=========================== short test summary info ============================\n"
        f"FAILED tests/test_calc.py::test_add - assert {value} == 5\n"
        "1 failed in 0.03s\n"
    )


def _body_schedule(
    n: int, break_at: int, fix_at: int | None, break_again_at: int | None
) -> list[str]:
    """``add()`` body after each step 0..n."""
    bodies = [GOOD_BODY]
    cur = GOOD_BODY
    for i in range(1, n + 1):
        if i == break_at:
            cur = BAD_BODY
        elif i == fix_at:
            cur = GOOD_BODY
        elif i == break_again_at:
            cur = BAD_BODY_2
        bodies.append(cur)
    return bodies


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
    observe_at: int | None = None,
    break_again_at: int | None = None,
    token_growth: int = 0,
) -> Trace:
    """Create ``run_dir`` with a workdir, a shadow repo, and a saved trace of ``n_steps`` steps.

    ``noop_every=k`` makes every k-th step (other than ``break_at``/``fix_at``) a shell no-op, so
    its snapshot is deduplicated. ``git_workdir`` makes the workdir a git repo with one commit, to
    show that its ``.git`` is never touched. The suggested check goes to ``run_dir/check.txt``.

    ``observe_at`` turns that step into a shell step that shows the pytest failure of ``test_add``
    (exit code 1); the repo must be broken after it. ``break_again_at`` (after ``fix_at``) breaks
    ``add`` a second time. ``token_growth`` adds that many input tokens per step, as a growing
    context would (step ``i`` has ``100 + token_growth * (i - 1)`` input tokens).
    """
    run_dir = Path(run_dir)
    if not 1 <= break_at <= n_steps:
        raise ValueError("break_at must be within 1..n_steps")
    if fix_at is not None and not break_at < fix_at <= n_steps:
        raise ValueError("fix_at must be within break_at+1..n_steps")
    if break_again_at is not None and (fix_at is None or not fix_at < break_again_at <= n_steps):
        raise ValueError("break_again_at needs fix_at, and must be within fix_at+1..n_steps")
    bodies = _body_schedule(n_steps, break_at, fix_at, break_again_at)
    if observe_at is not None:
        if not break_at <= observe_at <= n_steps:
            raise ValueError("observe_at must be within break_at..n_steps")
        if bodies[observe_at] == GOOD_BODY:
            raise ValueError("observe_at must be a step after which the test fails")
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
        special = i in (break_at, fix_at, break_again_at, observe_at)
        reasoning = f"Step {i}."
        output, exit_code = "ok", None
        if i in (break_at, fix_at, break_again_at):
            old, body = body, bodies[i]
            tool, kind = "edit_file", ToolKind.EDIT
            args = {"path": "calc/__init__.py", "old": old, "new": body}
            reasoning = {break_at: "Simplify add().", fix_at: "Restore add()."}.get(
                i, "Rewrite add() again."
            )
            init_path.write_text(_init_source(body, comments))
            if i == observe_at:  # edit and run the test in one shell call
                tool, kind = "run_shell", ToolKind.SHELL
                expr = body.splitlines()[1].split("return ")[1]
                sed = f"sed -i 's/    return a . b/    return {expr}/' calc/__init__.py"
                args = {"command": f"{sed} && {OBSERVE_CMD}"}
                output, exit_code = pytest_failure_output(_BAD_VALUE[body]), 1
        elif i == observe_at:
            tool, kind = "run_shell", ToolKind.SHELL
            args = {"command": OBSERVE_CMD}
            output, exit_code = pytest_failure_output(_BAD_VALUE[body]), 1
        elif noop_every and i % noop_every == 0 and not special:
            tool, kind = "run_shell", ToolKind.SHELL
            args = {"command": "ls"}
            exit_code = 0
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
                output=output,
                exit_code=exit_code,
                reasoning=reasoning,
                snapshot=sha,
                tree=tree,
                tokens_in=TOKENS_IN + token_growth * (i - 1),
                tokens_out=TOKENS_OUT,
            )
        )

    meta = TraceMeta(
        run_id=run_dir.name,
        source=TraceSource.SYNTHETIC,
        workdir=workdir,
        base_snapshot=base,
        task_id="synthetic",
        sabotage_step=break_again_at or break_at,
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
        observe_at=args.observe_at,
        break_again_at=args.break_again_at,
        token_growth=args.token_growth,
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
    make.add_argument(
        "--observe-at", type=int, default=None, help="step whose shell output shows the failure"
    )
    make.add_argument(
        "--break-again-at", type=int, default=None, help="second, different break (needs --fix-at)"
    )
    make.add_argument("--token-growth", type=int, default=0, help="extra input tokens per step")
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
