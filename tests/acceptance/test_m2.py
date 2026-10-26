"""Acceptance checks for milestone M2 (recording, sabotage, report, detection lag, auto-oracle).

Black-box: every check drives the real CLI (`python -m agentbisect.cli ...`), the fake-LLM harness
(`python -m agentbisect.harness.agent --fake`), the Alan Code step handler
(`python -m agentbisect.hooks.opencode step`), or the plugin itself under `node`. Traces are read
back as raw JSON, not through the product's own loaders, wherever that is practical. Everything
lives in pytest temp dirs. No network, no credits: `ALAN_*` variables are stripped from every
subprocess and no test calls `create_client`.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from agentbisect import snapshot
from agentbisect.llm import ChatResponse, FakeLLM, ToolCall
from agentbisect.models import Step, ToolKind, Trace, TraceMeta, TraceSource, Usage

PROJECT = Path(__file__).resolve().parents[2]
TASKS = PROJECT / "tasks"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MUTATING = {"write", "edit", "shell"}


# --------------------------------------------------------------------------- helpers


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALAN_", "GIT_"))}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["COLUMNS"] = "200"
    return env


def py(
    *args: str, cwd: Path, stdin: str | None = None, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        input=stdin,
        timeout=timeout,
        env=clean_env(),
        cwd=cwd,
    )


def cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return py("-m", "agentbisect.cli", *args, cwd=cwd)


def git(*args: str, git_dir: Path | None = None, cwd: Path | None = None) -> str:
    cmd = ["git"]
    if git_dir is not None:
        cmd.append(f"--git-dir={git_dir}")
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=clean_env(), cwd=cwd)
    return out.stdout + out.stderr


def read_trace_raw(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trace.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines if x.strip()]


def bisect_json(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "bisect.json").read_text())


def make_fake_run_cli(tmp: Path, run_id: str = "fake-t01") -> Path:
    proc = py(
        "-m",
        "agentbisect.harness.agent",
        "--fake",
        "--runs-dir",
        str(tmp),
        "--run-id",
        run_id,
        cwd=tmp,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return tmp / run_id


# A tiny two-test project, built step by step with the real snapshot module.
PYTEST_INI = "[pytest]\ntestpaths = tests\npythonpath = .\naddopts = -p no:cacheprovider\n"
TWO_TESTS = """from app import a, b


def test_a():
    assert a() == 1


def test_b():
    assert b() == 2
"""


def app_source(a_val: int = 1, b_val: int = 2, extra: str = "") -> str:
    return f"def a():\n    return {a_val}\n\n\ndef b():\n    return {b_val}\n{extra}"


def build_run(
    run_dir: Path,
    n_steps: int,
    *,
    break_a_at: int | None = None,
    break_b_at: int | None = None,
    app_extra_at: dict[int, str] | None = None,
    overrides: dict[int, dict[str, Any]] | None = None,
) -> Path:
    """Run dir with ``n_steps`` write steps; ``test_a``/``test_b`` break at the given steps.

    ``overrides[i]`` replaces fields of step ``i`` (``output``, ``reasoning``, ``args``, ...).
    """
    workdir = run_dir / "workdir"
    (workdir / "tests").mkdir(parents=True)
    (workdir / "notes").mkdir()
    (workdir / "pytest.ini").write_text(PYTEST_INI)
    (workdir / "tests" / "test_two.py").write_text(TWO_TESTS)
    (workdir / "app.py").write_text(app_source())
    base = snapshot.init_shadow(run_dir, workdir)
    a_val, b_val, extra = 1, 2, ""
    steps: list[Step] = []
    for i in range(1, n_steps + 1):
        if i == break_a_at:
            a_val = 0
        if i == break_b_at:
            b_val = 0
        if app_extra_at and i in app_extra_at:
            extra += app_extra_at[i]
        touches_app = i in (break_a_at, break_b_at) or bool(app_extra_at and i in app_extra_at)
        if touches_app:
            path, content = "app.py", app_source(a_val, b_val, extra)
        else:
            path, content = f"notes/n{i:02d}.txt", f"note {i}\n"
        (workdir / path).write_text(content)
        sha, tree = snapshot.take_snapshot(run_dir, workdir, f"step {i}")
        fields: dict[str, Any] = {
            "index": i,
            "tool": "write_file",
            "kind": ToolKind.WRITE,
            "args": {"path": path, "content": content},
            "output": "ok",
            "reasoning": f"Step {i}.",
            "snapshot": sha,
            "tree": tree,
            "tokens_in": 100,
            "tokens_out": 50,
        }
        fields.update((overrides or {}).get(i, {}))
        steps.append(Step(**fields))
    meta = TraceMeta(
        run_id=run_dir.name,
        source=TraceSource.SYNTHETIC,
        workdir=workdir,
        base_snapshot=base,
        task_id="two",
        finished=True,
    )
    Trace(meta=meta, steps=steps).save(run_dir)
    return run_dir


# --------------------------------------------------------------------------- criterion 1


def test_c1_fake_run_has_one_snapshot_per_mutating_step(tmp_path: Path) -> None:
    run = make_fake_run_cli(tmp_path)
    steps = read_trace_raw(run)
    assert len(steps) >= 10
    assert [s["index"] for s in steps] == list(range(1, len(steps) + 1))

    kinds = {s["kind"] for s in steps}
    assert kinds >= MUTATING and "read" in kinds, kinds  # the run exercises both sides

    shadow = run / "shadow.git"
    meta = json.loads((run / "meta.json").read_text())
    TraceMeta.model_validate(meta)  # valid against the contract
    assert HEX40.match(meta["base_snapshot"])
    assert meta["run_id"] == run.name and meta["task_id"] == "t01"
    assert meta["source"] == "alan" and meta["finished"] is True
    assert Path(meta["workdir"]) == (run / "workdir").resolve()
    assert (run / "workdir").is_dir()
    assert git("cat-file", "-t", meta["base_snapshot"], git_dir=shadow).strip() == "commit"

    trees = [git("rev-parse", f"{meta['base_snapshot']}^{{tree}}", git_dir=shadow).strip()]
    for s in steps:
        if s["kind"] in MUTATING:
            assert s["snapshot"] and HEX40.match(s["snapshot"]), s["index"]
            assert git("cat-file", "-t", s["snapshot"], git_dir=shadow).strip() == "commit"
            assert s["tree"] and HEX40.match(s["tree"])
            real_tree = git("rev-parse", f"{s['snapshot']}^{{tree}}", git_dir=shadow).strip()
            assert real_tree == s["tree"], s["index"]
            trees.append(s["tree"])
        else:
            assert s["snapshot"] is None, (s["index"], s["kind"])
    # An edit step really changed the repo: its tree differs from the one before it.
    for s, before in zip(
        (x for x in steps if x["kind"] in MUTATING),
        trees[:-1],
        strict=True,
    ):
        if s["kind"] in ("write", "edit"):
            assert s["tree"] != before, s["index"]

    # The last snapshot equals the final workdir (caches aside).
    last = [s for s in steps if s["snapshot"]][-1]["snapshot"]
    restored = tmp_path / "restored"
    restored.mkdir()
    archive = subprocess.run(
        ["git", f"--git-dir={shadow}", "archive", "--format=tar", last],
        capture_output=True,
        timeout=60,
        env=clean_env(),
    )
    assert archive.returncode == 0
    subprocess.run(["tar", "-xf", "-", "-C", str(restored)], input=archive.stdout, check=True)
    mismatches = 0
    for path in restored.rglob("*"):
        if path.is_file():
            other = run / "workdir" / path.relative_to(restored)
            assert other.exists(), other
            if not filecmp.cmp(path, other, shallow=False):
                mismatches += 1
    assert mismatches == 0


# --------------------------------------------------------------------------- criterion 2


def resp(content: str, name: str, args: dict[str, Any], n: int) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=[ToolCall(id=f"call_{n}", name=name, arguments=args)],
        usage=Usage(tokens_in=1000 + 50 * n, tokens_out=40),
    )


def sabotage_script(at: int, offset: int) -> list[tuple[str, dict[str, Any], str]]:
    """Tool calls (name, args, reasoning). Step ``at`` is a write, or a read if ``offset == 1``.

    Steps before ``at`` alternate read and write; the sabotage must land on the first mutating
    step at or after ``at``, which is step ``at + offset``.
    """
    calls: list[tuple[str, dict[str, Any], str]] = []
    for i in range(1, at):
        if i % 2:
            calls.append(("read_file", {"path": "shop/errors.py"}, "Reading."))
        else:
            calls.append(
                ("write_file", {"path": f"scratch/n{i}.txt", "content": f"{i}\n"}, "Note.")
            )
    if offset:
        calls.append(("read_file", {"path": "shop/money.py"}, "Reading again."))
    calls.append(("write_file", {"path": f"scratch/n{at + offset}.txt", "content": "x\n"}, "Note."))
    calls.append(("write_file", {"path": "scratch/more.txt", "content": "y\n"}, "More."))
    calls.append(("run_shell", {"command": "python -m pytest -q"}, "Run the suite."))
    calls.append(("finish", {"summary": "done"}, "Done."))
    return calls


SABOTAGE_TASKS = [("s01", 3), ("s02", 6), ("s03", 9)]


def assert_sabotage_found(run: Path, expected_step: int, check: str | None) -> None:
    args = ["run", "--trace", str(run)]
    if check:
        args += ["--check", check]
    proc = cli(*args, cwd=run.parent)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    meta = json.loads((run / "meta.json").read_text())
    assert meta["sabotage_step"] == expected_step, meta
    report = bisect_json(run)
    assert report["culprits"], "bisect found nothing: the sabotage did not break any test"
    for c in report["culprits"]:
        assert c["bisect"]["status"] == "found", c["bisect"].get("reason")
        assert c["bisect"]["decisive_step"] == expected_step, (c["check"], c["bisect"])
    assert f"Step {expected_step}" in proc.stdout


@pytest.mark.parametrize("offset", [0, 1], ids=["at-step", "next-mutating"])
@pytest.mark.parametrize(("task_id", "at"), SABOTAGE_TASKS)
def test_c2_harness_sabotage_is_found_by_bisect(
    tmp_path: Path, task_id: str, at: int, offset: int
) -> None:
    from agentbisect.harness.agent import load_task, record

    task = load_task(TASKS / f"{task_id}.yaml")
    assert task.sabotage is not None and task.sabotage.at_step == at
    script = sabotage_script(at, offset)
    client = FakeLLM([resp(r, n, a, i) for i, (n, a, r) in enumerate(script, 1)], model="fake")
    run = record(task, client, tmp_path, model_alias="fake", run_id=f"{task_id}-h")
    expected = at + offset
    steps = read_trace_raw(run)
    assert steps[expected - 1]["kind"] == "write"
    assert_sabotage_found(run, expected, check=None)


def exec_opencode_tool(workdir: Path, tool: str, args: dict[str, Any]) -> tuple[str, dict]:
    """What OpenCode would do for a tool call, in the workdir."""
    if tool == "read":
        return (workdir / args["filePath"]).read_text(), {}
    if tool == "write":
        target = workdir / args["filePath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"])
        return "Wrote file successfully.", {}
    if tool == "edit":
        target = workdir / args["filePath"]
        text = target.read_text()
        assert text.count(args["oldString"]) == 1
        target.write_text(text.replace(args["oldString"], args["newString"]))
        return "Edit applied successfully.", {}
    proc = subprocess.run(
        args["command"],
        shell=True,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        env=clean_env(),
    )
    return proc.stdout + proc.stderr, {"exit": proc.returncode, "description": "run"}


def send_step(run: Path, n: int, tool: str, args: dict[str, Any]) -> None:
    """Execute a tool in the run's workdir, then report it to the step handler like the plugin."""
    workdir = Path(json.loads((run / "meta.json").read_text())["workdir"])
    output, metadata = exec_opencode_tool(workdir, tool, args)
    payload = {
        "tool": tool,
        "sessionID": "ses_test",
        "callID": f"call_{n}",
        "args": args,
        "title": tool,
        "output": output,
        "metadata": metadata,
    }
    proc = py(
        "-m",
        "agentbisect.hooks.opencode",
        "step",
        "--run-dir",
        str(run),
        cwd=run.parent,
        stdin=json.dumps(payload),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def to_opencode(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "read_file":
        return "read", {"filePath": args["path"]}
    if name == "write_file":
        return "write", {"filePath": args["path"], "content": args["content"]}
    return "bash", {"command": args["command"], "description": "run"}


@pytest.mark.parametrize("offset", [0, 1], ids=["at-step", "next-mutating"])
@pytest.mark.parametrize(("task_id", "at"), SABOTAGE_TASKS)
def test_c2_alan_code_sabotage_is_found_by_bisect(
    tmp_path: Path, task_id: str, at: int, offset: int
) -> None:
    run = tmp_path / f"{task_id}-oc"
    proc = cli(
        "record",
        "--task",
        str(TASKS / f"{task_id}.yaml"),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        run.name,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    n = 0
    for name, args, _ in sabotage_script(at, offset):
        if name == "finish":
            continue
        n += 1
        tool, oc_args = to_opencode(name, args)
        send_step(run, n, tool, oc_args)
    expected = at + offset
    steps = read_trace_raw(run)
    assert len(steps) == n
    assert steps[expected - 1]["kind"] == "write"
    assert_sabotage_found(run, expected, check=None)
    # The same answer with an explicit whole-suite check.
    assert_sabotage_found(run, expected, check="python -m pytest -q")


# --------------------------------------------------------------------------- criterion 3


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts[-1] += data


def parse_html(text: str) -> Tags:
    parser = Tags()
    parser.feed(text)
    return parser


def page_data(html: str) -> dict:
    """The JSON blob the report app draws, after checking it is the page's only data script."""
    parsed = parse_html(html)
    scripts = [attrs for tag, attrs in parsed.tags if tag == "script"]
    assert all("src" not in attrs for attrs in scripts)  # inline code only
    blobs = [a for a in scripts if a.get("type") == "application/json"]
    assert len(blobs) == 1 and blobs[0].get("id") == "data"
    blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert blob and "<" not in blob.group(1)  # nothing in it can close the element
    return json.loads(blob.group(1))


def assert_offline(html: str) -> None:
    parsed = parse_html(html)
    for tag, attrs in parsed.tags:
        for key, value in attrs.items():
            if key in ("src", "href", "action", "poster", "data", "srcset", "xlink:href"):
                assert value is not None
                v = value.strip().lower()
                assert not v.startswith(("http:", "https:", "//", "ftp:")), (tag, key, value)
        assert tag not in ("link", "iframe", "object", "embed", "img", "video", "audio"), tag
        if tag == "script":
            assert "src" not in attrs
    assert "@import" not in html
    assert not re.search(r"url\(\s*['\"]?\s*(https?:)?//", html, re.I)
    assert not re.search(r"https?://", html), re.findall(r".{20}https?://.{30}", html)[:3]
    assert "fonts.googleapis" not in html and "@font-face" not in html


def test_c3_report_is_offline_and_shows_verdict_timeline_diff(tmp_path: Path) -> None:
    run = make_fake_run_cli(tmp_path)
    proc = cli("run", "--trace", str(run), cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    before = {p.name for p in run.iterdir()}
    proc = cli("report", "--trace", str(run), cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = run / "report.html"
    assert out.is_file() and out.stat().st_size > 2000
    assert {p.name for p in run.iterdir()} - before == {"report.html"}  # one file, nothing else

    html = out.read_text()
    assert html.lstrip().lower().startswith("<!doctype html")
    assert_offline(html)
    page = page_data(html)  # the report is an app: its data is one JSON blob
    # Verdict: the decisive step, the test and the lag from bisect.json.
    culprit = bisect_json(run)["culprits"][0]
    t_err, t_obs = culprit["analysis"]["t_err"], culprit["analysis"]["t_obs"]
    assert (t_err, t_obs) == (8, 11)
    group = page["groups"][0]
    assert group["found"] and group["decisive"] == t_err
    assert group["sentences"][0].startswith(f"Step {t_err}")
    assert "broke test_add_duplicate_raises" in group["sentences"][0]
    assert f"step {t_obs}" in " ".join(group["sentences"])
    assert f"Step {t_err}" in html and "test_add_duplicate_raises" in html  # also as plain text
    # Replay and strip: one entry per step, the probes of the bisect, the decisive step marked.
    n_steps = len(read_trace_raw(run))
    assert len(page["steps"]) == n_steps
    frames = group["members"][0]["replay"]["frames"]
    assert [f["step"] for f in frames] == [p["step_index"] for p in culprit["bisect"]["probes"]]
    assert frames[-1]["lo"] == t_err - 1 and frames[-1]["hi"] == t_err
    # Diff of the decisive step.
    diff = page["steps"][t_err - 1]["diff"]
    assert "shop/catalog.py" in diff
    assert re.search(r"\n-\s+if product\.sku in self\._products:", diff)
    # The report states that the verdict does not come from an LLM.
    assert "No language model decides pass or fail" in html


def test_c3_trace_content_is_escaped(tmp_path: Path) -> None:
    payload = '<script>alert("XSS-{}")</script>'
    img = '<img src=x onerror=alert("IMG-{}")>'
    overrides = {
        2: {
            "output": payload.format("out") + img.format("out"),
            "reasoning": payload.format("rsn"),
        },
        4: {"args": {"path": "notes/n04.txt", "content": payload.format("arg")}},
        6: {"output": "</pre></details></section>" + payload.format("close")},
    }
    run = build_run(
        tmp_path / "xss",
        n_steps=8,
        break_a_at=5,
        app_extra_at={5: "# " + payload.format("diff") + "\n"},
        overrides=overrides,
    )
    proc = cli(
        "run",
        "--trace",
        str(run),
        "--check",
        "python -m pytest -q tests/test_two.py::test_a",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert bisect_json(run)["culprits"][0]["bisect"]["decisive_step"] == 5
    # Also plant text in bisect.json fields the report prints (the explanation is LLM text).
    data = bisect_json(run)
    data["culprits"][0]["explanation"] = payload.format("expl") + img.format("expl")
    (run / "bisect.json").write_text(json.dumps(data))
    proc = cli("report", "--trace", str(run), cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    html = (run / "report.html").read_text()
    assert "<script>alert(" not in html
    assert "<img" not in html  # only the escaped "&lt;img" form may appear
    parsed = parse_html(html)
    page = page_data(html)
    code = [s for s in parsed.scripts if not s.lstrip().startswith("{")]  # the page's own scripts
    assert code and all("XSS" not in s for s in code)  # run text never reaches script code
    assert all(t != "img" for t, _ in parsed.tags)
    assert all(not k.startswith("on") for _, attrs in parsed.tags for k in attrs)
    # It travels as escaped data, which the app draws as text: step output, reasoning, args, the
    # diff and the explanation all made it in.
    assert html.count("\\u003cscript\\u003e") >= 4
    text = json.dumps(page)
    assert text.count("<script>alert(") >= 4
    assert page["groups"][0]["explanation"]["text"].startswith("<script>alert(")
    assert "XSS-diff" in page["steps"][4]["diff"]
    assert_offline(html)


# --------------------------------------------------------------------------- criterion 4


def synthetic_run(tmp: Path, *flags: str, name: str = "syn") -> Path:
    out = tmp / name
    proc = py("-m", "agentbisect.synthetic", "make", "--out", str(out), *flags, cwd=tmp)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def run_synthetic(run: Path) -> dict[str, Any]:
    check = (run / "check.txt").read_text().strip()
    proc = cli("run", "--trace", str(run), "--check", check, cwd=run.parent)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = bisect_json(run)
    assert len(report["culprits"]) == 1
    return report["culprits"][0]


@pytest.mark.parametrize("growth", [0, 7], ids=["flat-tokens", "growing-tokens"])
def test_c4_detection_lag_and_wasted_tokens(tmp_path: Path, growth: int) -> None:
    run = synthetic_run(
        tmp_path,
        "--steps",
        "30",
        "--break-at",
        "9",
        "--observe-at",
        "21",
        "--token-growth",
        str(growth),
    )
    culprit = run_synthetic(run)
    steps = read_trace_raw(run)
    assert len(steps) == 30
    a = culprit["analysis"]
    assert culprit["bisect"]["decisive_step"] == 9
    assert (a["t_err"], a["t_obs"], a["lag"], a["censored"]) == (9, 21, 12, False)
    expected = sum(s["tokens_in"] + s["tokens_out"] for s in steps if s["index"] >= 10)
    assert a["wasted_tokens"] == expected
    assert a["total_tokens"] == sum(s["tokens_in"] + s["tokens_out"] for s in steps)
    assert a["steps_after_err"] == 21
    if growth == 0:
        assert expected == 21 * 150


def test_c4_no_observe_step_means_censored(tmp_path: Path) -> None:
    run = synthetic_run(tmp_path, "--steps", "30", "--break-at", "9", "--token-growth", "5")
    culprit = run_synthetic(run)
    steps = read_trace_raw(run)
    a = culprit["analysis"]
    assert culprit["bisect"]["decisive_step"] == 9
    assert a["censored"] is True
    assert a["t_obs"] is None and a["lag"] is None
    assert a["wasted_tokens"] == sum(
        s["tokens_in"] + s["tokens_out"] for s in steps if s["index"] >= 10
    )
    out = cli(
        "run", "--trace", str(run), "--check", (run / "check.txt").read_text().strip(), cwd=tmp_path
    )
    assert (
        "never" in out.stdout.lower()
        or "not noticed" in out.stdout.lower()
        or "censor" in out.stdout.lower()
    ), out.stdout


def test_c4_lag_zero_when_the_breaking_step_shows_the_failure(tmp_path: Path) -> None:
    run = synthetic_run(tmp_path, "--steps", "12", "--break-at", "5", "--observe-at", "5")
    a = run_synthetic(run)["analysis"]
    assert (a["t_err"], a["t_obs"], a["lag"], a["censored"]) == (5, 5, 0, False)


def test_c4_unrelated_failure_output_does_not_count_as_observed(tmp_path: Path) -> None:
    # Step 8 shows a failure of a different test; the agent never sees test_a fail.
    unrelated = (
        "F\n=== FAILURES ===\n___ test_other ___\n"
        "FAILED tests/test_other.py::test_other - assert 1 == 2\n1 failed in 0.01s\n"
    )
    run = build_run(
        tmp_path / "unrel",
        n_steps=10,
        break_a_at=3,
        overrides={
            8: {
                "tool": "run_shell",
                "kind": ToolKind.SHELL,
                "args": {"command": "python -m pytest -q tests/test_other.py"},
                "output": unrelated,
                "exit_code": 1,
            }
        },
    )
    proc = cli(
        "run",
        "--trace",
        str(run),
        "--check",
        "python -m pytest -q tests/test_two.py::test_a",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    a = bisect_json(run)["culprits"][0]["analysis"]
    assert a["t_err"] == 3 and a["censored"] is True and a["lag"] is None


# --------------------------------------------------------------------------- criterion 5


@pytest.mark.parametrize(("a_at", "b_at"), [(4, 11), (12, 3)])
def test_c5_auto_oracle_two_tests_two_culprits(tmp_path: Path, a_at: int, b_at: int) -> None:
    run = build_run(tmp_path / "two", n_steps=15, break_a_at=a_at, break_b_at=b_at)
    proc = cli("run", "--trace", str(run), cwd=tmp_path)  # no --check
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = bisect_json(run)
    assert report["auto_oracle"] is True
    culprits = report["culprits"]
    assert len(culprits) == 2, [c["check"] for c in culprits]
    by_test = {}
    for c in culprits:
        assert c["bisect"]["status"] == "found"
        name = "test_a" if "test_a" in c["check"] else "test_b"
        by_test[name] = c["bisect"]["decisive_step"]
    assert by_test == {"test_a": a_at, "test_b": b_at}
    # The terminal names both steps.
    assert f"Step {a_at}" in proc.stdout and f"Step {b_at}" in proc.stdout
    # The report page carries both verdicts.
    assert cli("report", "--trace", str(run), cwd=tmp_path).returncode == 0
    html = (run / "report.html").read_text()
    assert f"Step {a_at}" in html and f"Step {b_at}" in html
    assert "test_a" in html and "test_b" in html


def test_c5_auto_oracle_nothing_newly_failing(tmp_path: Path) -> None:
    run = build_run(tmp_path / "ok", n_steps=6)
    proc = cli("run", "--trace", str(run), cwd=tmp_path)
    assert bisect_json(run)["culprits"] == []
    assert "No failing check" in proc.stdout or "Nothing passed" in proc.stdout


# --------------------------------------------------------------------------- criterion 6


def tree_files(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and "__pycache__" not in rel.parts and ".pytest_cache" not in rel.parts:
            out[str(rel)] = p.read_bytes()
    return out


def record_t01(tmp: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """`agentbisect record --task tasks/t01.yaml` with the default (Alan Code) flow.

    Runs with the temp dir as cwd, so the default `runs/` is a temp dir and the project's own
    `runs/` is never touched.
    """
    before = (
        sorted(p.name for p in (PROJECT / "runs").glob("*")) if (PROJECT / "runs").exists() else []
    )
    proc = cli("record", "--task", str(TASKS / "t01.yaml"), "--run-id", "m2-t01", cwd=tmp)
    after = (
        sorted(p.name for p in (PROJECT / "runs").glob("*")) if (PROJECT / "runs").exists() else []
    )
    assert before == after, "record wrote into the project's runs/ directory"
    return proc, tmp / "runs" / "m2-t01"


def test_c6_record_default_flow_prepares_the_run(tmp_path: Path) -> None:
    proc, run = record_t01(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    workdir = run / "workdir"
    plugin = workdir / ".opencode" / "plugin" / "agentbisect.js"
    for path in (workdir, plugin, run / "PROMPT.txt", run / "meta.json", run / "shadow.git"):
        assert path.exists(), path
    # The workdir is a copy of the fixture repo.
    fixture = tree_files(TASKS / "fixture_repo")
    copied = {
        k: v for k, v in tree_files(workdir).items() if not k.startswith((".opencode", ".git/"))
    }
    assert fixture == copied
    # The workdir has its own git repo, so `git status` in it never walks up into another repo.
    assert (workdir / ".git").exists()
    top = git("rev-parse", "--show-toplevel", cwd=workdir).strip()
    assert Path(top).resolve() == workdir.resolve(), top
    # PROMPT.txt carries the task prompt.
    import yaml

    prompt = yaml.safe_load((TASKS / "t01.yaml").read_text())["prompt"]
    # The only allowed difference: `python` pinned to an interpreter path (commit f212c2e).
    saved = (run / "PROMPT.txt").read_text().strip()
    normalized = re.sub(r"`'?[^`]*?python'? -m pytest", "`python -m pytest", saved)
    assert normalized == prompt.strip()
    assert "-m pytest" in saved
    # meta.json is valid and points at the workdir.
    meta = TraceMeta.model_validate_json((run / "meta.json").read_text())
    assert meta.source is TraceSource.ALAN_CODE and meta.task_id == "t01"
    assert meta.workdir.resolve() == workdir.resolve()
    assert HEX40.match(meta.base_snapshot)
    assert (run / "trace.jsonl").exists() and read_trace_raw(run) == []
    # The terminal tells the human what to do: which folder to open, and the prompt.
    flat = re.sub(r"\s+", "", proc.stdout)
    assert str(workdir).replace(" ", "") in flat
    assert "PROMPT.txt" in proc.stdout
    # The plugin is wired to this run, and not excluded by accident from being loadable.
    text = plugin.read_text()
    assert "__AGENTBISECT_" not in text
    assert str(run.resolve()) in text and sys.executable in text
    assert "tool.execute.after" in text and "session.idle" in text
    # The plugin file is not in any snapshot, and writing under .opencode/ changes nothing.
    shadow = run / "shadow.git"
    base_files = git("ls-tree", "-r", "--name-only", meta.base_snapshot, git_dir=shadow).split()
    assert base_files and not any(f.startswith(".opencode") for f in base_files)
    assert "shop/catalog.py" in base_files
    assert not any(f == ".git" or f.startswith(".git/") for f in base_files)
    for sha in git("log", "--all", "--format=%H", git_dir=shadow).split():
        names = git("ls-tree", "-r", "--name-only", sha, git_dir=shadow).split()
        assert not any(f == ".git" or f.startswith(".git/") for f in names), sha
    (workdir / ".opencode" / "scratch.txt").write_text("noise")
    send_step(run, 1, "bash", {"command": "true", "description": "noop"})
    step = read_trace_raw(run)[0]
    assert step["snapshot"] == meta.base_snapshot  # same tree as base: deduped
    # Re-preparing the same run id is refused instead of overwriting.
    again = cli("record", "--task", str(TASKS / "t01.yaml"), "--run-id", "m2-t01", cwd=tmp_path)
    assert again.returncode != 0


NODE = shutil.which("node")

NODE_DRIVER = r"""
import { pathToFileURL } from "node:url";
const [pluginPath, workdir] = process.argv.slice(2);
import("node:fs").then(async (fs) => {
  const mod = await import(pathToFileURL(pluginPath).href);
  const hooks = await mod.AgentBisect({});
  const sid = "ses_node";
  const write = async (n, rel, content) => {
    fs.mkdirSync(workdir + "/" + rel.split("/").slice(0, -1).join("/"), { recursive: true });
    fs.writeFileSync(workdir + "/" + rel, content);
    await tooled(n, "write", { filePath: rel, content }, "Wrote file successfully.", {});
  };
  const edit = async (n, rel, oldS, newS) => {
    const text = fs.readFileSync(workdir + "/" + rel, "utf8");
    fs.writeFileSync(workdir + "/" + rel, text.replace(oldS, newS));
    await tooled(n, "edit", { filePath: rel, oldString: oldS, newString: newS }, "Edit applied successfully.", {});
  };
  // OpenCode emits the tool part as running, runs the tool, calls the hook, then emits completed.
  const tooled = async (n, tool, args, output, metadata) => {
    const base = { id: "tool" + n, messageID: "msg_a", sessionID: sid, type: "tool", callID: "c" + n, tool };
    await ev("message.part.updated", { part: { ...base, state: { status: "running", input: args } } });
    await hooks["tool.execute.after"](
      { tool, sessionID: sid, callID: "c" + n, args },
      { title: tool, output, metadata },
    );
    await ev("message.part.updated", { part: { ...base, state: { status: "completed", input: args, output, metadata, title: tool } } });
  };
  const part = (id, type, extra) => ({ id, messageID: "msg_a", sessionID: sid, type, ...extra });
  const ev = (type, properties) => hooks.event({ event: { type, properties } });
  await ev("session.created", { info: { id: sid } });
  await ev("message.updated", { info: { id: "msg_a", sessionID: sid, role: "assistant", modelID: "node-model", providerID: "alan", cost: 0.25,
    tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } } });

  const seq = [
    ["Adding a note.", () => write(1, "NOTES.md", "hello\n"), 1000, 20],
    ["Dropping the duplicate check.", () => edit(2, "shop/catalog.py",
      "        if product.sku in self._products:\n            raise DuplicateProductError(product.sku)\n", ""), 2000, 30],
    ["One more note.", () => write(3, "docs_extra.md", "more\n"), 3000, 25],
  ];
  let n = 0;
  for (const [text, run, tin, tout] of seq) {
    n += 1;
    await ev("message.part.updated", { part: part("s" + n, "step-start", {}) });
    await ev("message.part.updated", { part: part("t" + n, "text", { text }) });
    await run();
    await ev("message.part.updated", { part: part("f" + n, "step-finish", {
      tokens: { input: tin, output: tout, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0.01 }) });
  }
  // A failing shell step: the agent runs the test and sees the failure.
  n += 1;
  const out = "FAILED tests/test_catalog.py::test_add_duplicate_raises - Failed: DID NOT RAISE\n1 failed in 0.1s\n";
  await ev("message.part.updated", { part: part("s" + n, "step-start", {}) });
  await ev("message.part.updated", { part: part("t" + n, "text", { text: "Run the duplicate test." }) });
  await tooled(n, "bash", { command: "python -m pytest -q tests/test_catalog.py::test_add_duplicate_raises", description: "run" }, out, { exit: 1 });
  await ev("message.part.updated", { part: part("f" + n, "step-finish", {
    tokens: { input: 4000, output: 35, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0.01 }) });
  await ev("session.idle", { sessionID: sid });
  console.log("DONE " + n);
});
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_c6_plugin_runs_end_to_end_under_node(tmp_path: Path) -> None:
    proc, run = record_t01(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    workdir = run / "workdir"
    plugin = workdir / ".opencode" / "plugin" / "agentbisect.js"
    driver = tmp_path / "driver.mjs"
    driver.write_text(NODE_DRIVER)
    # Load the plugin file as an ES module. Plain `.js` needs Node's module detection; if this
    # Node lacks it, load a byte-identical `.mjs` copy.
    plugin_path = plugin
    probe = subprocess.run(
        [NODE, "--input-type=module", "-e", f"await import({json.dumps(plugin.as_uri())})"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        plugin_path = tmp_path / "agentbisect.mjs"
        shutil.copy(plugin, plugin_path)
    node = subprocess.run(
        [NODE, str(driver), str(plugin_path), str(workdir)],
        capture_output=True,
        text=True,
        timeout=300,
        env=clean_env(),
    )
    assert node.returncode == 0, node.stdout + node.stderr
    assert "DONE 4" in node.stdout
    # The plugin logged no errors.
    log = run / "plugin.log"
    assert not log.exists() or log.read_text().strip() == "", log.read_text()
    assert not (run / "hook.log").exists() or (run / "hook.log").read_text().strip() == "", (
        run / "hook.log"
    ).read_text()

    steps = read_trace_raw(run)
    assert [s["tool"] for s in steps] == ["write", "edit", "write", "bash"]
    assert [s["kind"] for s in steps] == ["write", "edit", "write", "shell"]
    assert all(s["snapshot"] and HEX40.match(s["snapshot"]) for s in steps)
    assert steps[3]["exit_code"] == 1
    assert "DID NOT RAISE" in steps[3]["output"]
    shadow = run / "shadow.git"
    snaps = [s["snapshot"] for s in steps]
    # The pytest step changes nothing, so its snapshot is the one before it (dedup by tree).
    assert snaps[3] == snaps[2] and len(set(snaps)) == 3
    for sha in snaps:
        files = git("ls-tree", "-r", "--name-only", sha, git_dir=shadow).split()
        assert files and not any(f.startswith(".opencode") for f in files)
    # Step 2 removed the duplicate check in the snapshot.
    cat1 = git("show", f"{snaps[0]}:shop/catalog.py", git_dir=shadow)
    cat2 = git("show", f"{snaps[1]}:shop/catalog.py", git_dir=shadow)
    assert (
        "DuplicateProductError(product.sku)" in cat1
        and "DuplicateProductError(product.sku)" not in cat2
    )

    # Events and finalize ran: reasoning and tokens filled in, finalize.json says hooks mode.
    assert (run / "events.jsonl").exists()
    finalize = json.loads((run / "finalize.json").read_text())
    assert finalize.get("mode") == "hooks", finalize
    steps = read_trace_raw(run)
    assert [s["reasoning"] for s in steps][:3] == [
        "Adding a note.",
        "Dropping the duplicate check.",
        "One more note.",
    ]
    assert [s["tokens_in"] for s in steps] == [1000, 2000, 3000, 4000]
    assert [s["tokens_out"] for s in steps] == [20, 30, 25, 35]

    # And bisect works on it: the edit at step 2 broke the test; the agent saw it at step 4.
    result = cli("run", "--trace", str(run), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    culprits = bisect_json(run)["culprits"]
    assert [c["bisect"]["decisive_step"] for c in culprits] == [2]
    a = culprits[0]["analysis"]
    assert (a["t_err"], a["t_obs"], a["lag"]) == (2, 4, 2)
    assert a["wasted_tokens"] == (3000 + 25) + (4000 + 35)


# --------------------------------------------------------------------------- criterion 7


def test_c7_cli_has_exactly_four_commands(tmp_path: Path) -> None:
    import typer.main

    from agentbisect.cli import app

    names = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
    assert names == {"record", "run", "report", "study"}
    out = cli("--help", cwd=tmp_path).stdout
    listed = set(re.findall(r"^│\s+([a-z]+)\s{2,}", out, flags=re.M))
    assert listed == {"record", "run", "report", "study"}, out
    for name in sorted(names):
        sub = cli(name, "--help", cwd=tmp_path)
        assert sub.returncode == 0, name


def project_git_state() -> dict[str, str]:
    status = [
        line
        for line in git("--no-optional-locks", "status", "--porcelain", cwd=PROJECT).splitlines()
        if "tests/acceptance/test_m2.py" not in line and not line.endswith(".env.example")
    ]
    return {
        "head": git("rev-parse", "HEAD", cwd=PROJECT).strip(),
        "refs": git("for-each-ref", "--format=%(refname) %(objectname)", cwd=PROJECT),
        "status": "\n".join(status),
        "runs": "\n".join(sorted(p.name for p in (PROJECT / "runs").glob("*")))
        if (PROJECT / "runs").exists()
        else "",
    }


def test_c7_project_git_and_runs_are_untouched(tmp_path: Path) -> None:
    before = project_git_state()
    run = make_fake_run_cli(tmp_path)
    assert cli("run", "--trace", str(run), cwd=tmp_path).returncode == 0
    assert cli("report", "--trace", str(run), cwd=tmp_path).returncode == 0
    proc, rec = record_t01(tmp_path)
    assert proc.returncode == 0
    for tool, args in (
        ("write", {"filePath": "x.txt", "content": "x\n"}),
        ("bash", {"command": "echo hi", "description": "run"}),
    ):
        send_step(rec, 1 if tool == "write" else 2, tool, args)
    assert cli("run", "--trace", str(rec), cwd=tmp_path).returncode in (0, 1)
    after = project_git_state()
    assert after == before


def test_c7_ruff_is_clean() -> None:
    ruff = shutil.which("ruff", path=str(Path(sys.executable).parent))
    if ruff is None:
        pytest.skip("ruff is not installed in this environment")
    check = subprocess.run(
        [ruff, "check", "."], cwd=PROJECT, capture_output=True, text=True, timeout=120
    )
    assert check.returncode == 0, check.stdout + check.stderr
    fmt = subprocess.run(
        [ruff, "format", "--check", "."], cwd=PROJECT, capture_output=True, text=True, timeout=120
    )
    assert fmt.returncode == 0, fmt.stdout + fmt.stderr


@pytest.fixture(autouse=True)
def _no_alan_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Belt and braces: no test may find Alan credentials, so none can spend credits."""
    for key in list(os.environ):
        if key.startswith("ALAN_"):
            monkeypatch.delenv(key)
    yield
