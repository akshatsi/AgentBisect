"""The only door to an LLM: an OpenAI-compatible chat client with a hard spend cap.

Every LLM call in AgentBisect goes through this module.

* ``OpenAICompatClient`` talks to Alan AI. NOTE: verify Alan's endpoint format when access
  arrives (path, auth header, ``usage`` field, tool-call shape). If it is not OpenAI-compatible,
  adapt ``OpenAICompatClient._post`` and ``_parse_response`` here and nowhere else.
* ``FakeLLM`` replays recorded responses. Unit tests use it. It never touches the network
  or the ledger.
* ``SpendLedger`` is a JSON file shared across processes (``fcntl`` lock). Before each request
  the client estimates the worst-case cost and reserves it in the ledger. If ledger + estimate
  would pass the cap, the request is refused with ``SpendCapExceeded`` and nothing is sent.
  After the response the reservation is replaced by the actual cost from ``usage``.
* A model with no known price is refused (``UnknownPriceError``). We never guess a price.

LLM output is never used to decide pass or fail anywhere in AgentBisect.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field

from agentbisect.models import LLMConfig, Usage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_PATH = PROJECT_ROOT / "tasks" / "models.yaml"
DEFAULT_LEDGER_PATH = PROJECT_ROOT / "runs" / ".spend.json"
DEFAULT_SPEND_CAP_USD = 8.00
DEFAULT_MAX_TOKENS = 4096

#: A reservation older than this is treated as left behind by a crashed process and dropped.
PENDING_TTL_S = 900.0

# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base class for errors from this module."""


class ConfigError(LLMError):
    """Missing or invalid configuration."""


class SpendCapExceeded(LLMError):
    """The request would push total spend past the cap. Nothing was sent."""


class UnknownPriceError(LLMError):
    """The model has no price in models.yaml, so its cost cannot be bounded. Nothing was sent."""


class LedgerError(LLMError):
    """The spend ledger is unreadable. We refuse to spend rather than reset it."""


class FakeLLMExhausted(LLMError):
    """FakeLLM was asked for more responses than it holds."""


class APIError(LLMError):
    """The API returned an error that retrying will not fix, or retries ran out."""


# --------------------------------------------------------------------------- data


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    parse_error: str | None = Field(
        default=None, description="Set when the model's argument string was not valid JSON."
    )


class ChatResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelEntry(BaseModel):
    """One row of tasks/models.yaml."""

    alias: str
    display_name: str = ""
    api_id: str | None = None
    role: str = ""
    price_in_per_1m: float | None = Field(default=None, ge=0)
    price_out_per_1m: float | None = Field(default=None, ge=0)
    tool_calls: bool | None = None

    @property
    def has_price(self) -> bool:
        return self.price_in_per_1m is not None and self.price_out_per_1m is not None

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        if self.price_in_per_1m is None or self.price_out_per_1m is None:
            raise UnknownPriceError(f"model '{self.alias}' has no price in models.yaml")
        return (tokens_in * self.price_in_per_1m + tokens_out * self.price_out_per_1m) / 1_000_000


# --------------------------------------------------------------------------- models.yaml


def load_models(path: Path | None = None) -> dict[str, ModelEntry]:
    """Read tasks/models.yaml into ``{alias: ModelEntry}``."""
    path = path or DEFAULT_MODELS_PATH
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    rows = data.get("models") or {}
    return {alias: ModelEntry(alias=alias, **(row or {})) for alias, row in rows.items()}


def find_model(models: Mapping[str, ModelEntry], name: str) -> ModelEntry | None:
    """Find an entry by alias (case-insensitive), then by API id."""
    lowered = name.lower()
    for alias, entry in models.items():
        if alias.lower() == lowered:
            return entry
    for entry in models.values():
        if entry.api_id is not None and entry.api_id == name:
            return entry
    return None


# --------------------------------------------------------------------------- config


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``.env`` file: ``KEY=VALUE`` lines, ``#`` comments, optional quotes."""
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def load_config(
    env_file: Path | None = Path(".env"),
    model_alias: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    models_path: Path | None = None,
) -> LLMConfig:
    """Build an ``LLMConfig`` from ``ALAN_*`` variables.

    Real environment variables win; ``env_file`` fills the gaps. ``model_alias`` (or, failing
    that, ``ALAN_MODEL``) is looked up in tasks/models.yaml; a name that is not an alias is
    used as a raw API id. An alias whose ``api_id`` is still null raises ``ConfigError``.
    """
    env = dict(parse_env_file(env_file)) if env_file is not None else {}
    env.update({k: v for k, v in (os.environ if environ is None else environ).items() if v})

    def need(key: str) -> str:
        value = env.get(key, "").strip()
        if not value:
            raise ConfigError(f"{key} is not set (environment or .env)")
        return value

    base_url = need("ALAN_BASE_URL")
    api_key = need("ALAN_API_KEY")
    name = model_alias or need("ALAN_MODEL")

    models = load_models(models_path)
    entry = find_model(models, name)
    if entry is None:
        model = name
    elif entry.api_id is None:
        raise ConfigError(
            f"alias '{entry.alias}' has no api_id yet; fill tasks/models.yaml from the probe"
        )
    else:
        model = entry.api_id

    raw_cap = env.get("ALAN_SPEND_CAP_USD", "").strip()
    try:
        cap = float(raw_cap) if raw_cap else DEFAULT_SPEND_CAP_USD
    except ValueError as e:
        raise ConfigError(f"ALAN_SPEND_CAP_USD is not a number: {raw_cap!r}") from e
    if not math.isfinite(cap) or cap <= 0:
        raise ConfigError(f"ALAN_SPEND_CAP_USD must be positive, got {cap}")
    return LLMConfig(base_url=base_url, api_key=api_key, model=model, spend_cap_usd=cap)


# --------------------------------------------------------------------------- spend ledger


class SpendLedger:
    """Total spend on disk, safe across processes.

    File shape: ``{"spent_usd": float, "calls": int, "pending": {id: {"usd", "ts", "pid"}}}``.
    ``pending`` holds worst-case reservations for requests in flight, so two processes cannot
    both pass the check and then both overspend.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_LEDGER_PATH

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                if raw:
                    try:
                        state = json.loads(raw)
                        float(state["spent_usd"])
                    except (ValueError, KeyError, TypeError) as e:
                        raise LedgerError(f"{self.path} is corrupt; fix or delete it: {e}") from e
                else:
                    state = {"spent_usd": 0.0, "calls": 0, "pending": {}}
                state.setdefault("calls", 0)
                state.setdefault("pending", {})
                yield state
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state, indent=2))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    def _pending_total(state: dict[str, Any], now: float) -> float:
        live = {k: v for k, v in state["pending"].items() if now - v["ts"] < PENDING_TTL_S}
        state["pending"] = live
        return sum(v["usd"] for v in live.values())

    def spent(self) -> float:
        """Actual spend recorded so far (excludes requests in flight)."""
        with self._locked() as state:
            return float(state["spent_usd"])

    def committed(self) -> float:
        """Actual spend plus live reservations."""
        with self._locked() as state:
            return float(state["spent_usd"]) + self._pending_total(state, time.time())

    def reserve(self, estimate_usd: float, cap_usd: float) -> str:
        """Hold ``estimate_usd`` or raise ``SpendCapExceeded``. Returns a reservation id."""
        with self._locked() as state:
            now = time.time()
            pending = self._pending_total(state, now)
            spent = float(state["spent_usd"])
            if spent + pending + estimate_usd > cap_usd + 1e-12:
                raise SpendCapExceeded(
                    f"spent ${spent:.4f} + in flight ${pending:.4f} + this request "
                    f"(estimated) ${estimate_usd:.4f} would pass the cap of ${cap_usd:.2f}"
                )
            rid = uuid.uuid4().hex
            state["pending"][rid] = {"usd": estimate_usd, "ts": now, "pid": os.getpid()}
            return rid

    def settle(self, rid: str, actual_usd: float) -> None:
        """Replace a reservation with the actual cost."""
        with self._locked() as state:
            state["pending"].pop(rid, None)
            state["spent_usd"] = float(state["spent_usd"]) + actual_usd
            state["calls"] += 1

    def release(self, rid: str) -> None:
        """Drop a reservation for a request that never produced a billable response."""
        with self._locked() as state:
            state["pending"].pop(rid, None)


# --------------------------------------------------------------------------- JSON fallback

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.DOTALL)


def _as_tool_call(obj: Any) -> ToolCall | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        return None
    args = obj.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return ToolCall(id=f"fallback-{uuid.uuid4().hex[:8]}", name=obj["tool"], arguments=args)


def _first_json_object(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if _as_tool_call(obj) is not None:
                return obj
    return None


def parse_json_action(text: str | None) -> ToolCall | None:
    """Parse the JSON fallback protocol for models without native tool calls.

    The model answers with ``{"tool": "<name>", "args": {...}}`` in a fenced block. A bare
    JSON object in the text is accepted too. Returns the first valid action, or ``None``.
    """
    if not text:
        return None
    for block in _FENCE.findall(text):
        obj = _first_json_object(block)
        if obj is not None:
            return _as_tool_call(obj)
    obj = _first_json_object(text)
    return _as_tool_call(obj) if obj is not None else None


def extract_tool_calls(response: ChatResponse) -> list[ToolCall]:
    """Native tool calls if the model made any, else one parsed from the text, else none."""
    if response.tool_calls:
        return list(response.tool_calls)
    call = parse_json_action(response.content)
    return [call] if call else []


# --------------------------------------------------------------------------- clients


class LLMClient(ABC):
    """What the harness and explainer talk to. Real and fake clients both implement this."""

    model: str

    def __init__(self) -> None:
        self.usage_total = Usage()

    @abstractmethod
    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send one chat request. ``tools`` uses the OpenAI function-tool schema."""

    def _add_usage(self, usage: Usage) -> None:
        self.usage_total = Usage(
            tokens_in=self.usage_total.tokens_in + usage.tokens_in,
            tokens_out=self.usage_total.tokens_out + usage.tokens_out,
            cost_usd=self.usage_total.cost_usd + usage.cost_usd,
        )


def estimate_tokens(text: str) -> int:
    """Rough token count: characters / 4, rounded up."""
    return math.ceil(len(text) / 4)


class OpenAICompatClient(LLMClient):
    """Chat client for an OpenAI-compatible endpoint (``POST {base_url}/chat/completions``).

    Verify Alan's endpoint format when access arrives; change only this class if it differs.
    """

    def __init__(
        self,
        config: LLMConfig,
        price: ModelEntry | None,
        ledger: SpendLedger | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> None:
        super().__init__()
        self.config = config
        self.model = config.model
        self.price = price
        self.ledger = ledger or SpendLedger()
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._sleep = sleep
        self.default_max_tokens = default_max_tokens
        self.temperature = temperature
        self._http = httpx.Client(timeout=config.timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    # -- cost ------------------------------------------------------------

    def estimate_cost(self, body: Mapping[str, Any]) -> float:
        """Worst-case cost: prompt tokens (chars / 4) plus the full ``max_tokens`` reply."""
        if self.price is None or not self.price.has_price:
            raise UnknownPriceError(
                f"no price for model '{self.model}' in tasks/models.yaml; refusing to call it"
            )
        prompt = json.dumps({"messages": body["messages"], "tools": body.get("tools")}, default=str)
        return self.price.cost(estimate_tokens(prompt), int(body["max_tokens"]))

    # -- request ---------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"

        estimate = self.estimate_cost(body)  # raises UnknownPriceError before anything is sent
        rid = self.ledger.reserve(estimate, self.config.spend_cap_usd)  # raises SpendCapExceeded
        try:
            data = self._post(body)
        except BaseException:
            self.ledger.release(rid)
            raise
        response = self._parse_response(data, estimate)
        self.ledger.settle(rid, response.usage.cost_usd)
        self._add_usage(response.usage)
        return response

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        last: str = ""
        for attempt in range(self.max_retries + 1):
            delay = self.backoff_s * (2**attempt)
            try:
                r = self._http.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    retry_after = r.headers.get("retry-after", "")
                    if retry_after.replace(".", "", 1).isdigit():
                        delay = max(delay, min(float(retry_after), 60.0))
                elif r.status_code >= 400:
                    raise APIError(f"HTTP {r.status_code}: {r.text[:500]}")
                else:
                    try:
                        data = r.json()
                    except ValueError as e:
                        raise APIError(f"response was not JSON: {r.text[:200]}") from e
                    if not isinstance(data, dict):
                        raise APIError(f"unexpected response shape: {str(data)[:200]}")
                    return data
            if attempt < self.max_retries:
                self._sleep(delay)
        raise APIError(f"gave up after {self.max_retries + 1} attempts; last error: {last}")

    def _parse_response(self, data: dict[str, Any], estimate_usd: float) -> ChatResponse:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise APIError(f"no choices in response: {str(data)[:200]}") from e
        calls = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            args: dict[str, Any] = {}
            error = None
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        error = f"arguments are not a JSON object: {raw_args[:100]}"
                except ValueError as e:
                    error = f"arguments are not valid JSON: {e}"
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call-{i}",
                    name=fn.get("name", ""),
                    arguments=args,
                    parse_error=error,
                )
            )
        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        if isinstance(tokens_in, int) and isinstance(tokens_out, int):
            assert self.price is not None  # estimate_cost already checked
            cost = self.price.cost(tokens_in, tokens_out)
        else:
            # No usage reported: charge the worst-case estimate, never zero.
            tokens_in, tokens_out, cost = 0, 0, estimate_usd
        return ChatResponse(
            content=message.get("content"),
            tool_calls=calls,
            usage=Usage(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost),
            raw=data,
        )


class FakeLLM(LLMClient):
    """Replays recorded ``ChatResponse``s in order. Free: no network, no ledger.

    ``requests`` keeps every ``(messages, tools)`` pair it was asked, for assertions.
    ``usage_total`` counts tokens but always reports ``cost_usd == 0``.
    """

    def __init__(self, responses: Iterable[ChatResponse | Mapping[str, Any]], model: str = "fake"):
        super().__init__()
        self.model = model
        self._responses = [
            r if isinstance(r, ChatResponse) else ChatResponse.model_validate(r) for r in responses
        ]
        self._next = 0
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []

    @classmethod
    def from_jsonl(cls, path: Path, model: str = "fake") -> FakeLLM:
        lines = Path(path).read_text().splitlines()
        return cls([ChatResponse.model_validate_json(x) for x in lines if x.strip()], model)

    @property
    def remaining(self) -> int:
        return len(self._responses) - self._next

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.requests.append(
            ([dict(m) for m in messages], [dict(t) for t in tools] if tools else None)
        )
        if self._next >= len(self._responses):
            raise FakeLLMExhausted(f"FakeLLM has no response #{self._next + 1}")
        response = self._responses[self._next].model_copy(deep=True)
        self._next += 1
        self._add_usage(
            Usage(tokens_in=response.usage.tokens_in, tokens_out=response.usage.tokens_out)
        )
        return response


def dump_responses(path: Path, responses: Iterable[ChatResponse]) -> None:
    """Write responses as JSONL, the format ``FakeLLM.from_jsonl`` reads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.model_dump_json() + "\n" for r in responses))


# --------------------------------------------------------------------------- factory


def create_client(
    model_alias: str | None = None,
    *,
    env_file: Path | None = Path(".env"),
    environ: Mapping[str, str] | None = None,
    models_path: Path | None = None,
    ledger_path: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    **client_kwargs: Any,
) -> OpenAICompatClient:
    """Config + price lookup + ledger, wired together. The ledger path can also come from
    ``ALAN_SPEND_LEDGER``; the default is ``runs/.spend.json`` under the project root."""
    config = load_config(env_file, model_alias, environ=environ, models_path=models_path)
    models = load_models(models_path)
    entry = find_model(models, model_alias) if model_alias else None
    if entry is None:
        entry = find_model(models, config.model)
    if ledger_path is None:
        env_path = (environ if environ is not None else os.environ).get("ALAN_SPEND_LEDGER")
        ledger_path = Path(env_path) if env_path else DEFAULT_LEDGER_PATH
    return OpenAICompatClient(
        config, entry, SpendLedger(ledger_path), transport=transport, **client_kwargs
    )
