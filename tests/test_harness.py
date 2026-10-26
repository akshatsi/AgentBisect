import json
from pathlib import Path

import pytest

from agentbisect import snapshot
from agentbisect.bisect import bisect
from agentbisect.harness.agent import load_task, make_fake_run, record
from agentbisect.llm import ChatResponse, FakeLLM, SpendCapExceeded, ToolCall
from agentbisect.models import (
    MUTATING_KINDS,
    BisectStatus,
    OracleConfig,
    ToolKind,
    Trace,
    TraceSource,
    Usage,
)

ROOT = Path(__file__).resolve().parent.parent
BREAK_CHECK = "python -m pytest tests/test_catalog.py::test_add_duplicate_raises -q"


def resp(tool: str, text: str | None = "", tin: int = 100, tout: int = 10, **args) -> ChatResponse:
    return ChatResponse(
        content=text,
        tool_calls=[ToolCall(id=f"c-{tool}", name=tool, arguments=args)],
        usage=Usage(tokens_in=tin, tokens_out=tout),
    )


def finish() -> ChatResponse:
    return resp("finish", "all done", summary="done")


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_fake_run(tmp_path_factory.mktemp("runs"))


# --------------------------------------------------------------------------- fake t01 run


def test_fake_run_has_one_snapshot_per_mutating_step(fake_run: Path):
    trace = Trace.load(fake_run)
    assert trace.n == 14
    assert trace.meta.source is TraceSource.ALAN
    assert trace.meta.finished
    assert trace.meta.task_id == "t01" and trace.meta.model == "fake"
    for s in trace.steps:
        assert (s.snapshot is not None) == (s.kind in MUTATING_KINDS), s.index
        assert s.tokens_in > 0
    known = set(snapshot.tree_map(fake_run))
    assert {s.snapshot for s in trace.steps if s.snapshot} <= known
    assert trace.steps[2].exit_code == 0  # baseline suite passes
    # dedup: the passing test runs do not change the tree
    assert trace.steps[6].snapshot == trace.steps[5].snapshot


def test_fake_run_reasoning_and_outputs_recorded(fake_run: Path):
    trace = Trace.load(fake_run)
    assert trace.steps[0].reasoning == "I will start by reading the catalog module."
    failing = trace.steps[10]
    assert failing.exit_code == 1
    assert "test_add_duplicate_raises" in failing.output
    assert "1 failed, 54 passed" in failing.output


def test_fake_run_bisects_to_the_scripted_break(fake_run: Path):
    trace = Trace.load(fake_run)
    result = bisect(trace, fake_run, OracleConfig(check=BREAK_CHECK))
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == 8


def test_user_dir_and_fixture_untouched(fake_run: Path):
    assert "update_price" not in (ROOT / "tasks/fixture_repo/shop/catalog.py").read_text()
    assert not list((ROOT / "tasks/fixture_repo").rglob("test_catalog_update.py"))


# --------------------------------------------------------------------------- loop behaviour


def run(tmp_path: Path, responses: list[ChatResponse], task_id: str = "t01", **kw) -> Path:
    task = load_task(ROOT / "tasks" / f"{task_id}.yaml")
    return record(task, FakeLLM(responses), tmp_path, run_id="r", **kw)


def test_meta_and_trace_written_and_loadable(tmp_path: Path):
    d = run(tmp_path, [resp("read_file", "look", path="shop/errors.py"), finish()])
    trace = Trace.load(d)
    assert [s.tool for s in trace.steps] == ["read_file", "finish"]
    assert trace.steps[0].kind is ToolKind.READ and trace.steps[0].snapshot is None
    assert trace.steps[1].kind is ToolKind.FINISH
    assert trace.meta.finished and trace.meta.base_snapshot
    assert (d / "shadow.git").is_dir() and (d / "workdir" / "shop").is_dir()
    record_json = json.loads((d / "record.json").read_text())
    assert record_json["stop_reason"] == "finished"


def test_tokens_come_from_usage(tmp_path: Path):
    d = run(tmp_path, [resp("read_file", tin=500, tout=20, path="shop/errors.py"), finish()])
    s = Trace.load(d).steps[0]
    assert (s.tokens_in, s.tokens_out) == (500, 20)


def test_max_steps_stops_unfinished(tmp_path: Path):
    task = load_task(ROOT / "tasks/t01.yaml").model_copy(update={"max_steps": 3})
    reads = [resp("read_file", path="shop/errors.py") for _ in range(10)]
    d = record(task, FakeLLM(reads), tmp_path, run_id="r")
    trace = Trace.load(d)
    assert trace.n == 3 and not trace.meta.finished
    assert json.loads((d / "record.json").read_text())["stop_reason"] == "max_steps"


def test_malformed_args_count_as_a_step_and_the_agent_sees_the_error(tmp_path: Path):
    bad = ChatResponse(
        content="try",
        tool_calls=[ToolCall(id="c1", name="edit_file", arguments={}, parse_error="bad json")],
    )
    client = FakeLLM([bad, resp("edit_file", path="shop/errors.py"), finish()])
    task = load_task(ROOT / "tasks/t01.yaml")
    d = record(task, client, tmp_path, run_id="r")
    trace = Trace.load(d)
    assert [s.tool for s in trace.steps] == ["edit_file", "edit_file", "finish"]
    assert "could not parse arguments" in trace.steps[0].output
    assert "'old' must be a string" in trace.steps[1].output
    # the error went back to the model as a tool message
    last_messages, _ = client.requests[1]
    assert last_messages[-1]["role"] == "tool"
    assert "could not parse" in last_messages[-1]["content"]
    # a failed edit changes nothing, so its snapshot equals the base
    assert trace.steps[0].tree == trace.steps[1].tree


def test_unknown_tool_is_a_step_with_kind_other(tmp_path: Path):
    d = run(tmp_path, [resp("teleport", where="mars"), finish()])
    s = Trace.load(d).steps[0]
    assert s.kind is ToolKind.OTHER and "unknown tool" in s.output and s.snapshot is None


def test_no_tool_call_is_nudged_once_then_stops(tmp_path: Path):
    talk = ChatResponse(content="I think I am done.", usage=Usage(tokens_in=70, tokens_out=7))
    client = FakeLLM([resp("read_file", path="shop/errors.py"), talk, talk])
    d = record(load_task(ROOT / "tasks/t01.yaml"), client, tmp_path, run_id="r")
    trace = Trace.load(d)
    assert trace.n == 1 and not trace.meta.finished
    assert json.loads((d / "record.json").read_text())["stop_reason"] == "no_tool_call"
    assert "without a tool call" in client.requests[2][0][-1]["content"]


def test_nudge_then_recovery_carries_tokens_to_next_step(tmp_path: Path):
    talk = ChatResponse(content="hmm", usage=Usage(tokens_in=70, tokens_out=7))
    d = run(tmp_path, [talk, resp("read_file", tin=100, tout=10, path="shop/errors.py"), finish()])
    s = Trace.load(d).steps[0]
    assert (s.tokens_in, s.tokens_out) == (170, 17)


def test_only_first_tool_call_per_turn_runs(tmp_path: Path):
    two = ChatResponse(
        content="both",
        tool_calls=[
            ToolCall(id="a", name="read_file", arguments={"path": "shop/errors.py"}),
            ToolCall(id="b", name="read_file", arguments={"path": "shop/money.py"}),
        ],
    )
    client = FakeLLM([two, finish()])
    d = record(load_task(ROOT / "tasks/t01.yaml"), client, tmp_path, run_id="r")
    assert Trace.load(d).n == 2
    tool_msgs = [m for m in client.requests[1][0] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]


def test_json_fallback_protocol_without_native_tools(tmp_path: Path):
    text = 'Reading.\n```json\n{"tool": "read_file", "args": {"path": "shop/errors.py"}}\n```'
    fin = '```json\n{"tool": "finish", "args": {"summary": "x"}}\n```'
    client = FakeLLM([ChatResponse(content=text), ChatResponse(content=fin)])
    d = record(load_task(ROOT / "tasks/t01.yaml"), client, tmp_path, run_id="r", native_tools=False)
    trace = Trace.load(d)
    assert [s.tool for s in trace.steps] == ["read_file", "finish"]
    assert trace.steps[0].reasoning == "Reading."
    first_messages, tools = client.requests[0]
    assert tools is None and "no native tool calls" in first_messages[0]["content"]
    assert client.requests[1][0][-1]["role"] == "user"


def test_spend_cap_saves_partial_trace_and_reraises(tmp_path: Path):
    class Capped(FakeLLM):
        def chat(self, messages, tools=None, *, max_tokens=None):
            if len(self.requests) >= 2:
                raise SpendCapExceeded("cap")
            return super().chat(messages, tools, max_tokens=max_tokens)

    client = Capped(
        [resp("read_file", path="shop/errors.py"), resp("read_file", path="shop/money.py")]
    )
    with pytest.raises(SpendCapExceeded) as info:
        record(load_task(ROOT / "tasks/t01.yaml"), client, tmp_path, run_id="r")
    run_dir = info.value.run_dir
    assert run_dir == tmp_path / "r"
    trace = Trace.load(run_dir)
    assert trace.n == 2 and not trace.meta.finished
    assert json.loads((run_dir / "record.json").read_text())["stop_reason"] == "spend_cap"


def test_existing_run_dir_is_refused(tmp_path: Path):
    run(tmp_path, [finish()])
    with pytest.raises(FileExistsError):
        run(tmp_path, [finish()])


def test_run_id_defaults_to_task_and_model(tmp_path: Path):
    task = load_task(ROOT / "tasks/t01.yaml")
    d = record(task, FakeLLM([finish()]), tmp_path, model_alias="flash")
    assert d.name.startswith("t01-flash-")


def test_cost_comes_from_client_usage(tmp_path: Path):
    class Priced(FakeLLM):
        def chat(self, messages, tools=None, *, max_tokens=None):
            out = super().chat(messages, tools, max_tokens=max_tokens)
            self._add_usage(Usage(cost_usd=0.01))
            return out

    task = load_task(ROOT / "tasks/t01.yaml")
    d = record(
        task, Priced([resp("read_file", path="shop/errors.py"), finish()]), tmp_path, run_id="r"
    )
    assert Trace.load(d).meta.cost_usd == pytest.approx(0.02)


# --------------------------------------------------------------------------- sabotage

SABOTAGE_CHECK = "python -m pytest tests/test_pricing.py -q"


def sabotage_script() -> list[ChatResponse]:
    return [
        resp("read_file", "look", path="shop/inventory.py"),
        resp("read_file", "look", path="tests/test_inventory.py"),
        resp(
            "edit_file",
            "add reorder_list",
            path="shop/inventory.py",
            old="    def total_units(self) -> int:",
            new="    def reorder_list(self, target: int) -> list[tuple[str, int]]:\n"
            "        return []\n\n    def total_units(self) -> int:",
        ),
        resp("run_shell", "check", command="python -m pytest tests/test_inventory.py -q"),
        resp("read_file", "look", path="shop/pricing.py"),
        finish(),
    ]


def test_sabotage_lands_in_exactly_the_scripted_step(tmp_path: Path):
    d = run(tmp_path, sabotage_script(), task_id="s01")
    trace = Trace.load(d)
    assert trace.meta.sabotage_step == 3
    pricing = "shop/pricing.py"
    before = snapshot.read_file(d, trace.snapshot_at(2), pricing)
    at = snapshot.read_file(d, trace.snapshot_at(3), pricing)
    assert before is not None and "if qty >= min_qty:" in before
    assert at is not None and "if qty > min_qty:" in at
    assert json.loads((d / "record.json").read_text())["sabotage"].startswith("applied at step 3")


def test_bisect_returns_the_sabotage_step(tmp_path: Path):
    d = run(tmp_path, sabotage_script(), task_id="s01")
    trace = Trace.load(d)
    result = bisect(trace, d, OracleConfig(check=SABOTAGE_CHECK))
    assert result.status is BisectStatus.FOUND
    assert result.decisive_step == trace.meta.sabotage_step == 3


def test_sabotage_waits_for_the_next_mutating_step(tmp_path: Path):
    script = [
        resp("read_file", path="shop/inventory.py"),
        resp("read_file", path="shop/errors.py"),
        resp("read_file", path="shop/money.py"),  # step 3: read-only, no sabotage yet
        resp("read_file", path="shop/orders.py"),
        resp("run_shell", "tests", command="python -m pytest tests/test_money.py -q"),  # step 5
        finish(),
    ]
    d = run(tmp_path, script, task_id="s01")
    trace = Trace.load(d)
    assert trace.meta.sabotage_step == 5
    result = bisect(trace, d, OracleConfig(check=SABOTAGE_CHECK))
    assert result.decisive_step == 5


def test_sabotage_skipped_when_find_text_is_gone(tmp_path: Path):
    script = [
        resp(
            "write_file",
            "rewrite",
            path="shop/pricing.py",
            content="def bulk_discount_percent(qty):\n    return 0\n",
        ),
        resp("read_file", path="shop/errors.py"),
        resp("edit_file", "x", path="shop/errors.py", old="class", new="class"),
        finish(),
    ]
    d = run(tmp_path, script, task_id="s01")
    trace = Trace.load(d)
    assert trace.meta.sabotage_step is None
    note = json.loads((d / "record.json").read_text())["sabotage"]
    assert "not applied" in note and "find text not present" in note


def test_sabotage_never_runs_if_agent_finishes_early(tmp_path: Path):
    d = run(tmp_path, [resp("read_file", path="shop/errors.py"), finish()], task_id="s01")
    assert Trace.load(d).meta.sabotage_step is None
    assert "if qty >= min_qty:" in (d / "workdir/shop/pricing.py").read_text()


def test_non_sabotage_task_has_no_sabotage_note(tmp_path: Path):
    d = run(tmp_path, [finish()])
    assert json.loads((d / "record.json").read_text())["sabotage"] == "no sabotage"


def test_command_line_fake_run(tmp_path: Path, capsys):
    from agentbisect.harness.agent import main

    assert main(["--fake", "--runs-dir", str(tmp_path), "--run-id", "x"]) == 0
    assert (tmp_path / "x" / "trace.jsonl").exists()
    assert "fake run recorded" in capsys.readouterr().out
