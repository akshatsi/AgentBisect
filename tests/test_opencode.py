"""Alan Code recorder tests.

The event and payload shapes below follow what OpenCode 1.19.3 sends (hook inputs and bus events),
checked against strings in the binary. ASSUMPTION: no live Alan run has been seen yet, so field
names such as ``metadata.exit`` for bash are best guesses; the recorder accepts several spellings.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agentbisect import snapshot
from agentbisect.bisect import bisect
from agentbisect.harness.agent import load_task
from agentbisect.harness.tools import Toolbox
from agentbisect.hooks import opencode as oc
from agentbisect.llm import FakeLLM
from agentbisect.models import BisectStatus, OracleConfig, ToolKind, Trace, TraceSource

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SID = "ses_root"
BREAK_CHECK = "python -m pytest tests/test_catalog.py::test_add_duplicate_raises -q"

# --------------------------------------------------------------------------- simulator

OC_TOOL = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "run_shell": "bash",
}


def to_opencode_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "read_file":
        return {"filePath": args["path"]}
    if tool == "write_file":
        return {"filePath": args["path"], "content": args["content"]}
    if tool == "edit_file":
        return {"filePath": args["path"], "oldString": args["old"], "newString": args["new"]}
    return {"command": args["command"], "description": "run"}


def ev(type_: str, **props: Any) -> dict[str, Any]:
    return {"ts": 0, "type": type_, "properties": props}


def tool_part(n: int, call: str, tool: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"prt_tool{n}",
        "messageID": "msg_a1",
        "sessionID": SID,
        "type": "tool",
        "callID": call,
        "tool": tool,
        "state": state,
    }


class Session:
    """Plays a scripted agent against a prepared run, like OpenCode plus the plugin would."""

    def __init__(self, run_dir: Path, steps: Sequence[tuple[str, str, dict[str, Any], int, int]]):
        self.run_dir = run_dir
        self.box = Toolbox(Trace.load(run_dir).meta.workdir)
        self.script = steps  # (reasoning, tool, opencode args, tokens_in, tokens_out)
        self.events: list[dict[str, Any]] = [
            ev("session.created", info={"id": SID}),
            ev(
                "message.updated",
                info={"id": "msg_u1", "sessionID": SID, "role": "user"},
            ),
            ev(
                "message.part.updated",
                part={
                    "id": "prt_u1",
                    "messageID": "msg_u1",
                    "sessionID": SID,
                    "type": "text",
                    "text": "do it",
                },
            ),
            ev(
                "message.updated",
                info={
                    "id": "msg_a1",
                    "sessionID": SID,
                    "role": "assistant",
                    "modelID": "glm-test",
                    "providerID": "alan",
                    "cost": 0,
                    "tokens": {
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
            ),
        ]

    def play(self, send_steps: bool = True) -> None:
        for n, (reasoning, tool, args, tin, tout) in enumerate(self.script, 1):
            call = f"call_{n}"
            self.events += [
                ev(
                    "message.part.updated",
                    part={
                        "id": f"prt_s{n}",
                        "messageID": "msg_a1",
                        "sessionID": SID,
                        "type": "step-start",
                    },
                ),
                ev(
                    "message.part.updated",
                    part={
                        "id": f"prt_t{n}",
                        "messageID": "msg_a1",
                        "sessionID": SID,
                        "type": "text",
                        "text": reasoning,
                    },
                ),
                ev(
                    "message.part.updated",
                    part=tool_part(n, call, tool, {"status": "running", "input": args}),
                ),
            ]
            output, metadata = self.execute(tool, args)
            if send_steps:
                oc.record_step(
                    self.run_dir,
                    {
                        "tool": tool,
                        "sessionID": SID,
                        "callID": call,
                        "args": args,
                        "title": tool,
                        "output": output,
                        "metadata": metadata,
                    },
                )
            self.events += [
                ev(
                    "message.part.updated",
                    part=tool_part(
                        n,
                        call,
                        tool,
                        {
                            "status": "completed",
                            "input": args,
                            "output": output,
                            "metadata": metadata,
                            "title": tool,
                        },
                    ),
                ),
                ev(
                    "message.part.updated",
                    part={
                        "id": f"prt_f{n}",
                        "messageID": "msg_a1",
                        "sessionID": SID,
                        "type": "step-finish",
                        "tokens": {
                            "input": tin - 100,
                            "output": tout - 5,
                            "reasoning": 5,
                            "cache": {"read": 60, "write": 40},
                        },
                        "cost": 0.001,
                    },
                ),
            ]
        self.events.append(
            ev(
                "message.updated",
                info={
                    "id": "msg_a1",
                    "sessionID": SID,
                    "role": "assistant",
                    "modelID": "glm-test",
                    "providerID": "alan",
                    "cost": 0.5,
                    "tokens": {
                        "input": 1,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
            )
        )
        self.events.append(ev("session.idle", sessionID=SID))

    def execute(self, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if tool == "read":
            return self.box.execute("read_file", {"path": args["filePath"]}).output, {}
        if tool == "write":
            return self.box.execute(
                "write_file", {"path": args["filePath"], "content": args["content"]}
            ).output, {}
        if tool == "edit":
            res = self.box.execute(
                "edit_file",
                {"path": args["filePath"], "old": args["oldString"], "new": args["newString"]},
            )
            return res.output, {}
        res = self.box.execute("run_shell", {"command": args["command"]})
        return res.output, {"exit": res.exit_code, "description": "run"}

    def write_events(self) -> None:
        (self.run_dir / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.events)
        )


def t01_script() -> list[tuple[str, str, dict[str, Any], int, int]]:
    responses = FakeLLM.from_jsonl(FIXTURES / "fake_run_t01.jsonl")._responses
    out = []
    for r in responses:
        call = r.tool_calls[0]
        if call.name == "finish":
            continue
        out.append(
            (
                r.content or "",
                OC_TOOL[call.name],
                to_opencode_args(call.name, call.arguments),
                r.usage.tokens_in,
                r.usage.tokens_out,
            )
        )
    return out


def prepare(tmp_path: Path, task_id: str = "t01") -> Path:
    task = load_task(ROOT / "tasks" / f"{task_id}.yaml")
    return oc.prepare_opencode_run(task, tmp_path, "glm", "r1")


# --------------------------------------------------------------------------- prepare


def test_prepare_creates_run_with_plugin_and_prompt(tmp_path: Path):
    run_dir = prepare(tmp_path)
    trace = Trace.load(run_dir)
    assert trace.meta.source is TraceSource.ALAN_CODE
    assert trace.meta.model == "glm" and trace.meta.task_id == "t01"
    assert trace.n == 0 and trace.meta.base_snapshot
    assert (run_dir / "PROMPT.txt").read_text().startswith("The project is a small Python library")
    assert (run_dir / "shadow.git").is_dir()
    assert (trace.meta.workdir / "shop" / "catalog.py").exists()
    plugin = trace.meta.workdir / ".opencode" / "plugin" / "agentbisect.js"
    assert plugin.exists()


def test_rendered_plugin_has_baked_paths_and_no_placeholders(tmp_path: Path):
    run_dir = prepare(tmp_path)
    js = (Trace.load(run_dir).meta.workdir / ".opencode/plugin/agentbisect.js").read_text()
    assert "__AGENTBISECT" not in js
    assert json.dumps(sys.executable) in js
    assert json.dumps(str(run_dir.resolve())) in js
    assert json.dumps(str(ROOT)) in js
    assert "tool.execute.after" in js and "session.idle" in js and "export const" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_rendered_plugin_passes_node_check(tmp_path: Path):
    run_dir = prepare(tmp_path)
    js = (Trace.load(run_dir).meta.workdir / ".opencode/plugin/agentbisect.js").read_text()
    f = tmp_path / "plugin.mjs"
    f.write_text(js)
    proc = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_plugin_folder_is_not_snapshotted(tmp_path: Path):
    run_dir = prepare(tmp_path)
    trace = Trace.load(run_dir)
    sha, _ = snapshot.take_snapshot(run_dir, trace.meta.workdir, "again")
    assert sha == trace.meta.base_snapshot  # .opencode/ added no change


def test_existing_run_dir_refused(tmp_path: Path):
    prepare(tmp_path)
    with pytest.raises(FileExistsError):
        prepare(tmp_path)


# --------------------------------------------------------------------------- classification


def test_classify_and_snapshot_rules():
    assert oc.classify("write") is ToolKind.WRITE
    for t in ("edit", "multiedit", "patch", "apply_patch"):
        assert oc.classify(t) is ToolKind.EDIT
    assert oc.classify("bash") is ToolKind.SHELL
    for t in ("read", "glob", "grep", "list"):
        assert oc.classify(t) is ToolKind.READ
    for t in ("todowrite", "webfetch", "task", "foo_mcp"):
        assert oc.classify(t) is ToolKind.OTHER
    assert oc.should_snapshot("task", ToolKind.OTHER)
    assert oc.should_snapshot("foo_mcp", ToolKind.OTHER)
    assert not oc.should_snapshot("todowrite", ToolKind.OTHER)
    assert not oc.should_snapshot("webfetch", ToolKind.OTHER)
    assert not oc.should_snapshot("read", ToolKind.READ)


def test_exit_code_spellings():
    assert oc.exit_code_from({"exit": 2}) == 2
    assert oc.exit_code_from({"exitCode": 0}) == 0
    assert oc.exit_code_from({"exit_code": 5}) == 5
    assert oc.exit_code_from({"exit": None}) is None
    assert oc.exit_code_from({}) is None
    assert oc.exit_code_from(None) is None


# --------------------------------------------------------------------------- full session


@pytest.fixture(scope="module")
def t01_session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_dir = prepare(tmp_path_factory.mktemp("oc"))
    s = Session(run_dir, t01_script())
    s.play()
    s.write_events()
    assert oc.finalize_run(run_dir)["mode"] == "hooks"
    return run_dir


def test_steps_snapshots_and_exit_codes(t01_session: Path):
    trace = Trace.load(t01_session)
    assert trace.n == 13
    for s in trace.steps:
        assert (s.snapshot is not None) == (
            s.kind in (ToolKind.WRITE, ToolKind.EDIT, ToolKind.SHELL)
        )
        assert s.args["_session"] == SID and s.args["_call"] == f"call_{s.index}"
    assert [s.tool for s in trace.steps][:3] == ["read", "read", "bash"]
    assert trace.steps[2].exit_code == 0
    assert trace.steps[10].exit_code == 1 and "test_add_duplicate_raises" in trace.steps[10].output
    assert trace.steps[0].exit_code is None


def test_finalize_fills_reasoning_tokens_model_and_state(t01_session: Path):
    trace = Trace.load(t01_session)
    assert trace.steps[0].reasoning == "I will start by reading the catalog module."
    s = trace.steps[4]
    # step-finish tokens: input + cache.read + cache.write, output + reasoning
    assert (s.tokens_in, s.tokens_out) == (3800 - 100 + 100, 260)
    assert trace.meta.model == "glm"  # the alias given at prepare wins
    assert trace.meta.finished
    assert trace.meta.cost_usd == pytest.approx(0.5)
    assert json.loads((t01_session / "finalize.json").read_text())["per_step_snapshots"]


def test_bisect_finds_the_scripted_break(t01_session: Path):
    trace = Trace.load(t01_session)
    result = bisect(trace, t01_session, OracleConfig(check=BREAK_CHECK))
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == 8


def test_finalize_is_idempotent(t01_session: Path):
    before = (t01_session / "trace.jsonl").read_text()
    meta = (t01_session / "meta.json").read_text()
    oc.finalize_run(t01_session)
    oc.finalize_run(t01_session)
    assert (t01_session / "trace.jsonl").read_text() == before
    assert (t01_session / "meta.json").read_text() == meta


def test_repeated_call_id_is_ignored(t01_session: Path):
    n = Trace.load(t01_session).n
    assert oc.record_step(t01_session, {"tool": "bash", "callID": "call_3", "output": "x"}) is None
    assert Trace.load(t01_session).n == n


# --------------------------------------------------------------------------- sabotage


def test_sabotage_injected_in_the_step_handler(tmp_path: Path):
    run_dir = prepare(tmp_path, "s01")
    inv = "shop/inventory.py"
    script = [
        ("look", "read", {"filePath": inv}, 500, 20),
        ("look", "read", {"filePath": "tests/test_inventory.py"}, 600, 20),
        (
            "add",
            "edit",
            {
                "filePath": inv,
                "oldString": "    def total_units(self) -> int:",
                "newString": "    def reorder_list(self, target):\n        return []\n\n    def total_units(self) -> int:",
            },
            700,
            80,
        ),
        ("check", "bash", {"command": "python -m pytest tests/test_inventory.py -q"}, 800, 20),
    ]
    s = Session(run_dir, script)
    s.play()
    s.write_events()
    oc.finalize_run(run_dir)
    trace = Trace.load(run_dir)
    assert trace.meta.sabotage_step == 3
    result = bisect(trace, run_dir, OracleConfig(check="python -m pytest tests/test_pricing.py -q"))
    assert result.decisive_step == 3
    state = json.loads((run_dir / "sabotage.json").read_text())
    assert state["done"] and state["step"] == 3


# --------------------------------------------------------------------------- tokens


def test_parallel_calls_share_one_request_without_double_counting(tmp_path: Path):
    run_dir = prepare(tmp_path)
    a = {"filePath": "shop/errors.py"}
    b = {"filePath": "shop/money.py"}
    for i, args in enumerate((a, b), 1):
        oc.record_step(
            run_dir,
            {"tool": "read", "sessionID": SID, "callID": f"c{i}", "args": args, "output": "x"},
        )
    events = [
        ev(
            "message.updated",
            info={"id": "m", "sessionID": SID, "role": "assistant", "modelID": "x"},
        ),
        ev("message.part.updated", part={"id": "p0", "messageID": "m", "type": "step-start"}),
        ev(
            "message.part.updated",
            part={"id": "p1", "messageID": "m", "type": "text", "text": "reading two files"},
        ),
        ev(
            "message.part.updated",
            part={
                "id": "p2",
                "messageID": "m",
                "type": "tool",
                "callID": "c1",
                "tool": "read",
                "state": {"status": "completed", "input": a, "output": "x"},
            },
        ),
        ev(
            "message.part.updated",
            part={
                "id": "p3",
                "messageID": "m",
                "type": "tool",
                "callID": "c2",
                "tool": "read",
                "state": {"status": "completed", "input": b, "output": "x"},
            },
        ),
        ev(
            "message.part.updated",
            part={
                "id": "p4",
                "messageID": "m",
                "type": "step-finish",
                "tokens": {
                    "input": 1000,
                    "output": 50,
                    "reasoning": 10,
                    "cache": {"read": 200, "write": 0},
                },
            },
        ),
    ]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    oc.finalize_run(run_dir)
    s1, s2 = Trace.load(run_dir).steps
    assert (s1.tokens_in, s1.tokens_out) == (1200, 60)
    assert (s2.tokens_in, s2.tokens_out) == (0, 0)
    assert s1.reasoning == "reading two files" and s2.reasoning is None


def test_message_level_tokens_used_when_no_step_finish(tmp_path: Path):
    run_dir = prepare(tmp_path)
    oc.record_step(run_dir, {"tool": "read", "callID": "c1", "args": {}, "output": "x"})
    events = [
        ev(
            "message.updated",
            info={
                "id": "m",
                "role": "assistant",
                "modelID": "x",
                "tokens": {
                    "input": 10,
                    "output": 5,
                    "reasoning": 0,
                    "cache": {"read": 3, "write": 2},
                },
            },
        ),
        ev(
            "message.part.updated",
            part={
                "id": "p2",
                "messageID": "m",
                "type": "tool",
                "callID": "c1",
                "tool": "read",
                "state": {"status": "completed", "input": {}, "output": "x"},
            },
        ),
    ]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    oc.finalize_run(run_dir)
    s = Trace.load(run_dir).steps[0]
    assert (s.tokens_in, s.tokens_out) == (15, 5)
    assert not Trace.load(run_dir).meta.finished  # no session.idle seen


# --------------------------------------------------------------------------- fallback


def test_events_only_fallback_rebuilds_steps_and_takes_one_final_snapshot(tmp_path: Path):
    run_dir = prepare(tmp_path)
    s = Session(run_dir, t01_script())
    s.play(send_steps=False)  # tool.execute.after never fired
    s.write_events()
    assert Trace.load(run_dir).n == 0
    summary = oc.finalize_run(run_dir)
    assert summary["mode"] == "events_only" and not summary["per_step_snapshots"]
    trace = Trace.load(run_dir)
    assert trace.n == 13
    snaps = [(x.index, x.snapshot) for x in trace.steps if x.snapshot]
    assert len(snaps) == 1
    last_mutating = max(
        x.index for x in trace.steps if x.kind in (ToolKind.WRITE, ToolKind.EDIT, ToolKind.SHELL)
    )
    assert snaps[0][0] == last_mutating
    assert trace.steps[10].exit_code == 1 and "test_add_duplicate_raises" in trace.steps[10].output
    assert trace.steps[4].reasoning == "Now I add update_price before `skus`."
    # the final state carries the break, so the one-snapshot trace is still readable by bisect
    result = bisect(trace, run_dir, OracleConfig(check=BREAK_CHECK))
    assert result.status is BisectStatus.FOUND


def test_finalize_with_no_events_changes_nothing(tmp_path: Path):
    run_dir = prepare(tmp_path)
    oc.finalize_run(run_dir)
    assert Trace.load(run_dir).n == 0


# --------------------------------------------------------------------------- command line


def test_cli_exits_zero_and_logs_errors(tmp_path: Path):
    run_dir = prepare(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    base = [sys.executable, "-m", "agentbisect.hooks.opencode"]
    bad = subprocess.run(
        [*base, "step", "--run-dir", str(run_dir)],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert bad.returncode == 0 and bad.stdout == ""
    assert "step failed" in (run_dir / "hook.log").read_text()
    good = subprocess.run(
        [*base, "step", "--run-dir", str(run_dir)],
        input=json.dumps(
            {
                "tool": "bash",
                "callID": "z",
                "args": {"command": "ls"},
                "output": "x",
                "metadata": {"exit": 0},
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert good.returncode == 0
    assert Trace.load(run_dir).steps[0].exit_code == 0
    fin = subprocess.run(
        [*base, "finalize", "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert fin.returncode == 0 and (run_dir / "finalize.json").exists()


# --------------------------------------------------------------------------- the real plugin under node


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_plugin_runs_under_node_and_records(tmp_path: Path):
    run_dir = prepare(tmp_path)
    workdir = Trace.load(run_dir).meta.workdir
    (workdir / "marker.txt").write_text("changed by the agent\n")
    plugin = tmp_path / "agentbisect.mjs"
    plugin.write_text((workdir / ".opencode/plugin/agentbisect.js").read_text())
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        f"""
import {{ AgentBisect }} from {json.dumps(str(plugin))}
const hooks = await AgentBisect({{ directory: {json.dumps(str(workdir))} }})
await hooks.event({{ event: {{ type: "message.updated", properties: {{ info: {{ id: "m", role: "assistant", modelID: "node-model" }} }} }} }})
await hooks.event({{ event: {{ type: "message.part.updated", properties: {{ part: {{ id: "t", messageID: "m", type: "text", text: "writing a marker" }} }} }} }})
await hooks.event({{ event: {{ type: "message.part.updated", properties: {{ part: {{ id: "k", messageID: "m", type: "tool", callID: "n1", tool: "write", state: {{ status: "completed", input: {{}}, output: "ok" }} }} }} }} }})
await hooks.event({{ event: {{ type: "server.heartbeat", properties: {{}} }} }})
await hooks["tool.execute.after"]({{ tool: "write", sessionID: "s", callID: "n1", args: {{ filePath: "marker.txt" }} }}, {{ title: "write", output: "ok", metadata: {{}} }})
await hooks["tool.execute.after"](null, null)  // must not throw
await hooks.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "s" }} }} }})
"""
    )
    proc = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    trace = Trace.load(run_dir)
    assert trace.n == 1 and trace.steps[0].tool == "write" and trace.steps[0].snapshot
    assert trace.steps[0].reasoning == "writing a marker"
    assert trace.meta.finished
    lines = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
    types = [x["type"] for x in lines]
    assert "server.heartbeat" not in types and types[-1] == "session.idle"
    # the waiting text part was written before the tool part that followed it
    parts = [x["properties"]["part"]["type"] for x in lines if x["type"] == "message.part.updated"]
    assert parts == ["text", "tool"]


# --------------------------------------------------------------------------- recorded sample


def test_sample_events_file_parses(tmp_path: Path):
    """tests/fixtures/opencode_events_sample.jsonl: events of the t01 session above (hand-built
    from OpenCode's event shapes; replace it with a real capture once one exists)."""
    sample = FIXTURES / "opencode_events_sample.jsonl"
    if not sample.exists():  # regenerate: delete the file and run this test
        run_dir = prepare(tmp_path)
        s = Session(run_dir, t01_script())
        s.play(send_steps=False)
        s.write_events()
        shutil.copy(run_dir / "events.jsonl", sample)
    run_dir = prepare(tmp_path / "again")
    shutil.copy(sample, run_dir / "events.jsonl")
    calls = oc._Calls(oc.read_events(run_dir))
    assert len(calls.call_order) == 13
    assert calls.model() == "glm-test"
    assert calls.calls["call_5"]["reasoning"] == "Now I add update_price before `skus`."
