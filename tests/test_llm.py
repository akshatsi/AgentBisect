import json
import threading
from pathlib import Path

import httpx
import pytest

from agentbisect.llm import (
    APIError,
    ChatResponse,
    ConfigError,
    FakeLLM,
    FakeLLMExhausted,
    LedgerError,
    ModelEntry,
    OpenAICompatClient,
    SpendCapExceeded,
    SpendLedger,
    ToolCall,
    UnknownPriceError,
    create_client,
    dump_responses,
    extract_tool_calls,
    find_model,
    load_config,
    load_models,
    parse_env_file,
    parse_json_action,
)
from agentbisect.models import LLMConfig, Usage

# --------------------------------------------------------------------------- helpers

MODELS_YAML = """
models:
  cheap:
    display_name: Cheap One
    role: study
    api_id: vendor/cheap-1
    price_in_per_1m: 1.0
    price_out_per_1m: 2.0
    tool_calls: true
  pending:
    role: study
    api_id: null
    price_in_per_1m: null
    price_out_per_1m: null
  noprice:
    role: study
    api_id: vendor/noprice
    price_in_per_1m: null
    price_out_per_1m: null
"""

ENV = {
    "ALAN_BASE_URL": "https://alan.test/v1",
    "ALAN_API_KEY": "sk-test",
    "ALAN_MODEL": "cheap",
}


@pytest.fixture
def models_path(tmp_path: Path) -> Path:
    p = tmp_path / "models.yaml"
    p.write_text(MODELS_YAML)
    return p


def _config(cap: float = 8.0, model: str = "vendor/cheap-1") -> LLMConfig:
    return LLMConfig(
        base_url="https://alan.test/v1", api_key="sk-test", model=model, spend_cap_usd=cap
    )


CHEAP = ModelEntry(
    alias="cheap", api_id="vendor/cheap-1", price_in_per_1m=1.0, price_out_per_1m=2.0
)


def _ok_body(tokens_in: int = 100, tokens_out: int = 50, **message) -> dict:
    message = message or {"content": "hello"}
    return {
        "choices": [{"message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def _client(handler, tmp_path: Path, *, cap: float = 8.0, price=CHEAP, **kw) -> OpenAICompatClient:
    return OpenAICompatClient(
        _config(cap),
        price,
        SpendLedger(tmp_path / "spend.json"),
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
        **kw,
    )


MSGS = [{"role": "user", "content": "hi"}]

# --------------------------------------------------------------------------- config


def test_parse_env_file(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n\nA=1\nexport B=\"two words\"\nC='x'\nD=val # trailing\nnot a line\nE=\n"
    )
    assert parse_env_file(p) == {"A": "1", "B": "two words", "C": "x", "D": "val", "E": ""}
    assert parse_env_file(tmp_path / "missing") == {}


def test_load_config_from_env_file(tmp_path: Path, models_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "ALAN_BASE_URL=https://alan.test/v1\nALAN_API_KEY=sk-file\nALAN_MODEL=cheap\n"
        "ALAN_SPEND_CAP_USD=3.5\n"
    )
    cfg = load_config(p, environ={}, models_path=models_path)
    assert cfg.base_url == "https://alan.test/v1"
    assert cfg.api_key == "sk-file"
    assert cfg.model == "vendor/cheap-1"
    assert cfg.spend_cap_usd == 3.5


def test_environment_beats_env_file_and_cap_defaults_to_eight(tmp_path: Path, models_path: Path):
    p = tmp_path / ".env"
    p.write_text("ALAN_BASE_URL=https://file/v1\nALAN_API_KEY=sk-file\nALAN_MODEL=cheap\n")
    cfg = load_config(p, environ={"ALAN_API_KEY": "sk-env"}, models_path=models_path)
    assert cfg.api_key == "sk-env"
    assert cfg.base_url == "https://file/v1"
    assert cfg.spend_cap_usd == 8.00


def test_load_config_requires_url_and_key(tmp_path: Path, models_path: Path):
    with pytest.raises(ConfigError, match="ALAN_BASE_URL"):
        load_config(None, environ={"ALAN_API_KEY": "k", "ALAN_MODEL": "cheap"})
    with pytest.raises(ConfigError, match="ALAN_API_KEY"):
        load_config(None, environ={"ALAN_BASE_URL": "u", "ALAN_MODEL": "cheap"})


def test_blank_values_in_env_file_count_as_missing(tmp_path: Path, models_path: Path):
    p = tmp_path / ".env"
    p.write_text("ALAN_BASE_URL=\nALAN_API_KEY=\nALAN_MODEL=cheap\n")
    with pytest.raises(ConfigError):
        load_config(p, environ={}, models_path=models_path)


def test_bad_cap_is_rejected(models_path: Path):
    for bad in ("abc", "0", "-1", "nan"):
        with pytest.raises(ConfigError):
            load_config(None, environ={**ENV, "ALAN_SPEND_CAP_USD": bad}, models_path=models_path)


def test_alias_argument_beats_alan_model(models_path: Path):
    env = {**ENV, "ALAN_MODEL": "something-else"}
    cfg = load_config(None, "CHEAP", environ=env, models_path=models_path)  # case-insensitive
    assert cfg.model == "vendor/cheap-1"


def test_raw_api_id_passes_through(models_path: Path):
    cfg = load_config(None, environ={**ENV, "ALAN_MODEL": "org/raw-model"}, models_path=models_path)
    assert cfg.model == "org/raw-model"


def test_alias_with_null_api_id_is_an_error(models_path: Path):
    with pytest.raises(ConfigError, match="probe"):
        load_config(None, "pending", environ=ENV, models_path=models_path)


def test_find_model_by_alias_or_api_id(models_path: Path):
    models = load_models(models_path)
    assert find_model(models, "Cheap").alias == "cheap"
    assert find_model(models, "vendor/cheap-1").alias == "cheap"
    assert find_model(models, "nope") is None


def test_shipped_models_yaml_lists_every_alias_and_guesses_nothing():
    models = load_models()
    wanted = {"qwen", "flash", "glm", "kimi", "nano", "lightning"}
    assert wanted <= set(models)
    spares = {a for a, m in models.items() if m.role == "spare"}
    assert len(spares) == 5
    # Until the probe runs, no model may carry a guessed id or price.
    assert all(m.api_id is None and not m.has_price for m in models.values())


# --------------------------------------------------------------------------- FakeLLM


def test_fake_replays_in_order_and_records_requests():
    tc = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    fake = FakeLLM(
        [
            ChatResponse(content="one", usage=Usage(tokens_in=10, tokens_out=5, cost_usd=1.0)),
            ChatResponse(tool_calls=[tc]),
            {"content": "three"},
        ]
    )
    assert fake.chat(MSGS).content == "one"
    second = fake.chat(MSGS, tools=[{"type": "function"}])
    assert second.tool_calls[0].name == "read_file"
    assert fake.remaining == 1
    assert fake.chat(MSGS).content == "three"
    assert len(fake.requests) == 3
    assert fake.requests[1][1] == [{"type": "function"}]
    assert fake.usage_total.tokens_in == 10
    assert fake.usage_total.cost_usd == 0.0


def test_fake_raises_when_exhausted():
    fake = FakeLLM([ChatResponse(content="only")])
    fake.chat(MSGS)
    with pytest.raises(FakeLLMExhausted):
        fake.chat(MSGS)


def test_fake_returns_copies():
    fake = FakeLLM([ChatResponse(content="x")])
    r = fake.chat(MSGS)
    r.content = "changed"
    assert FakeLLM([ChatResponse(content="x")]).chat(MSGS).content == "x"


def test_fake_from_jsonl_roundtrip(tmp_path: Path):
    responses = [
        ChatResponse(content="a", usage=Usage(tokens_in=1, tokens_out=2, cost_usd=0.1)),
        ChatResponse(tool_calls=[ToolCall(id="1", name="finish", arguments={"summary": "ok"})]),
    ]
    p = tmp_path / "fixtures" / "r.jsonl"
    dump_responses(p, responses)
    fake = FakeLLM.from_jsonl(p)
    assert [fake.chat(MSGS) for _ in range(2)] == responses


def test_fake_does_not_touch_the_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    FakeLLM([ChatResponse(content="x")]).chat(MSGS)
    assert not list(tmp_path.iterdir())


# --------------------------------------------------------------------------- JSON fallback


def test_parse_fenced_json_action():
    text = 'I will read it.\n```json\n{"tool": "read_file", "args": {"path": "a.py"}}\n```\n'
    call = parse_json_action(text)
    assert call is not None
    assert (call.name, call.arguments) == ("read_file", {"path": "a.py"})


def test_parse_unlabeled_fence_and_bare_json_and_missing_args():
    assert parse_json_action('```\n{"tool": "finish", "args": {}}\n```').name == "finish"
    assert parse_json_action('Sure: {"tool": "run_shell", "args": {"cmd": "ls"}} done').name == (
        "run_shell"
    )
    call = parse_json_action('```json\n{"tool": "finish"}\n```')
    assert call is not None and call.arguments == {}


def test_parse_skips_non_action_blocks():
    text = '```json\n{"note": 1}\n```\nthen\n```json\n{"tool": "x", "args": {"a": 1}}\n```'
    assert parse_json_action(text).name == "x"


@pytest.mark.parametrize(
    "text",
    [None, "", "no json here", '```json\n{"tool": 5}\n```', '{"tool": "x", "args": [1]}', "{bad"],
)
def test_parse_rejects_garbage(text):
    assert parse_json_action(text) is None


def test_extract_prefers_native_calls():
    native = ToolCall(id="1", name="edit_file", arguments={})
    resp = ChatResponse(content='{"tool": "finish", "args": {}}', tool_calls=[native])
    assert extract_tool_calls(resp) == [native]
    assert extract_tool_calls(ChatResponse(content='{"tool": "finish", "args": {}}'))[0].name == (
        "finish"
    )
    assert extract_tool_calls(ChatResponse(content="just talking")) == []


# --------------------------------------------------------------------------- real client


def test_chat_parses_native_tool_calls_and_cost(tmp_path: Path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_ok_body(
                1000,
                500,
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "finish", "arguments": "{oops"},
                    },
                ],
            ),
        )

    client = _client(handler, tmp_path)
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    resp = client.chat(MSGS, tools=tools)
    assert seen["url"] == "https://alan.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "vendor/cheap-1"
    assert seen["body"]["tools"] == tools
    assert resp.tool_calls[0].arguments == {"path": "a.py"}
    assert resp.tool_calls[1].parse_error is not None
    assert resp.usage.tokens_in == 1000
    assert resp.usage.cost_usd == pytest.approx(1000 * 1.0 / 1e6 + 500 * 2.0 / 1e6)
    assert client.ledger.spent() == pytest.approx(0.002)
    assert client.usage_total.tokens_out == 500


def test_cap_refusal_happens_before_sending(tmp_path: Path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_ok_body())

    # Worst case: 4096 max_tokens at $2/M = $0.008192 plus the prompt. A $0.005 cap must refuse.
    client = _client(handler, tmp_path, cap=0.005)
    with pytest.raises(SpendCapExceeded):
        client.chat(MSGS)
    assert calls == []
    assert client.ledger.spent() == 0.0
    assert client.ledger.committed() == 0.0


def test_cap_is_enforced_as_spend_accumulates(tmp_path: Path):
    def handler(request):
        return httpx.Response(200, json=_ok_body(1_000_000, 0))  # $1.00 actual each

    client = _client(handler, tmp_path, cap=2.005)
    client.chat(MSGS)
    client.chat(MSGS)
    with pytest.raises(SpendCapExceeded):
        client.chat(MSGS)
    assert client.ledger.spent() == pytest.approx(2.0)


def test_ledger_is_shared_between_client_instances(tmp_path: Path):
    def handler(request):
        return httpx.Response(200, json=_ok_body(1_000_000, 0))

    a = _client(handler, tmp_path, cap=2.005)
    b = _client(handler, tmp_path, cap=2.005)
    a.chat(MSGS)
    b.chat(MSGS)
    assert a.ledger.spent() == pytest.approx(2.0)
    with pytest.raises(SpendCapExceeded):
        a.chat(MSGS)
    with pytest.raises(SpendCapExceeded):
        b.chat(MSGS)


def test_unknown_price_is_refused_without_sending(tmp_path: Path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_ok_body())

    no_price = ModelEntry(alias="noprice", api_id="vendor/cheap-1")
    for price in (no_price, None):
        client = _client(handler, tmp_path, price=price)
        with pytest.raises(UnknownPriceError):
            client.chat(MSGS)
    assert calls == []
    assert not (tmp_path / "spend.json").exists()


def test_create_client_refuses_null_price_models(tmp_path: Path, models_path: Path):
    client = create_client(
        "noprice",
        env_file=None,
        environ=ENV,
        models_path=models_path,
        ledger_path=tmp_path / "s.json",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())),
    )
    with pytest.raises(UnknownPriceError):
        client.chat(MSGS)


def test_create_client_wires_price_and_ledger(tmp_path: Path, models_path: Path):
    client = create_client(
        None,
        env_file=None,
        environ={**ENV, "ALAN_SPEND_CAP_USD": "1.5"},
        models_path=models_path,
        ledger_path=tmp_path / "s.json",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body(10, 10))),
    )
    assert client.config.spend_cap_usd == 1.5
    assert client.chat(MSGS).usage.cost_usd == pytest.approx(30 / 1e6)
    assert (tmp_path / "s.json").exists()


def test_retries_on_429_and_5xx_then_succeeds(tmp_path: Path):
    statuses = iter([429, 503, 200])
    sleeps: list[float] = []

    def handler(request):
        code = next(statuses)
        if code == 200:
            return httpx.Response(200, json=_ok_body())
        return httpx.Response(
            code, text="busy", headers={"retry-after": "3"} if code == 429 else {}
        )

    client = _client(handler, tmp_path)
    client._sleep = sleeps.append
    assert client.chat(MSGS).content == "hello"
    assert len(sleeps) == 2
    assert sleeps[0] >= 3.0  # honours Retry-After
    assert sleeps[1] == 2.0  # backoff_s * 2**1


def test_gives_up_after_retries_and_releases_the_reservation(tmp_path: Path):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(500, text="boom")

    client = _client(handler, tmp_path, max_retries=2)
    with pytest.raises(APIError, match="gave up"):
        client.chat(MSGS)
    assert n["calls"] == 3
    assert client.ledger.committed() == 0.0


def test_transport_errors_are_retried(tmp_path: Path):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json=_ok_body())

    assert _client(handler, tmp_path).chat(MSGS).content == "hello"
    assert n["calls"] == 2


def test_client_error_is_not_retried(tmp_path: Path):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(400, text="bad request")

    client = _client(handler, tmp_path)
    with pytest.raises(APIError, match="400"):
        client.chat(MSGS)
    assert n["calls"] == 1
    assert client.ledger.committed() == 0.0


def test_every_request_has_a_timeout(tmp_path: Path):
    client = OpenAICompatClient(
        LLMConfig(base_url="u", api_key="k", model="m", timeout_s=7.5),
        CHEAP,
        SpendLedger(tmp_path / "s.json"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ok_body())),
    )
    assert client._http.timeout.read == 7.5


def test_missing_usage_is_charged_at_the_estimate(tmp_path: Path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = _client(handler, tmp_path)
    resp = client.chat(MSGS)
    assert resp.usage.cost_usd > 0
    assert client.ledger.spent() == pytest.approx(resp.usage.cost_usd)


# --------------------------------------------------------------------------- ledger


def test_ledger_reserve_counts_requests_in_flight(tmp_path: Path):
    ledger = SpendLedger(tmp_path / "l.json")
    rid = ledger.reserve(1.0, cap_usd=1.5)
    with pytest.raises(SpendCapExceeded):
        ledger.reserve(1.0, cap_usd=1.5)
    ledger.settle(rid, 0.25)
    ledger.reserve(1.0, cap_usd=1.5)
    assert ledger.spent() == pytest.approx(0.25)


def test_ledger_threads_never_overspend(tmp_path: Path):
    path = tmp_path / "l.json"
    granted = []

    def worker():
        try:
            SpendLedger(path).reserve(1.0, cap_usd=5.0)
            granted.append(1)
        except SpendCapExceeded:
            pass

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 5


def test_corrupt_ledger_refuses_instead_of_resetting(tmp_path: Path):
    path = tmp_path / "l.json"
    path.write_text("{not json")
    with pytest.raises(LedgerError):
        SpendLedger(path).reserve(0.01, cap_usd=8.0)
    assert path.read_text() == "{not json"
