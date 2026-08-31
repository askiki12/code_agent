"""OpenAI-compatible chat client with streaming and tool-call parsing."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import requests


class LLMError(Exception):
    pass


class _Retryable(LLMError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    heuristic: bool = False


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage | None = None


def _nonneg_int(v) -> int:
    return v if isinstance(v, int) and v >= 0 else 0


def parse_usage(data: dict | None) -> Usage | None:
    if not isinstance(data, dict):
        return None
    prompt = data.get("prompt_tokens")
    if not isinstance(prompt, int) or prompt < 0:
        return None
    details = data.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=_nonneg_int(data.get("completion_tokens")),
        total_tokens=_nonneg_int(data.get("total_tokens")),
        cached_tokens=_nonneg_int(details.get("cached_tokens")),
    )


@dataclass
class _StreamAccumulator:
    content: str = ""
    _calls: dict[int, dict[str, str]] = field(default_factory=dict)
    usage: dict | None = None

    def feed(self, delta: dict) -> None:
        if isinstance(delta.get("content"), str):
            self.content += delta["content"]
        if isinstance(delta.get("usage"), dict):
            self.usage = delta["usage"]
        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def result(self) -> LLMResponse:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            calls.append(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["arguments"]),
                )
            )
        return LLMResponse(content=self.content, tool_calls=calls, usage=parse_usage(self.usage))


def parse_tool_arguments(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"malformed tool arguments JSON: {e}: {raw[:200]}") from e
    if not isinstance(data, dict):
        raise LLMError(f"tool arguments must be a JSON object, got {type(data).__name__}")
    return data


_MODEL_CONTEXT_WINDOW_PREFIXES = [
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1-mini", 128_000),
    ("gpt-4.1", 128_000),
    ("o1-mini", 128_000),
    ("o1", 200_000),
    ("o3-mini", 200_000),
    ("o3", 200_000),
    ("deepseek-chat", 64_000),
    ("deepseek-reasoner", 64_000),
]


def _get_json(url: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _table_context_window(model: str) -> int | None:
    for prefix, size in _MODEL_CONTEXT_WINDOW_PREFIXES:
        if model.startswith(prefix):
            return size
    return None


def resolve_context_window(
    model: str,
    base_url: str = "",
    api_key: str = "",
    *,
    get_json: Callable[[str, str], dict] | None = None,
    default: int = 1_000_000,
) -> int:
    """Resolve model context window: /models → name table → default. Never raises."""
    root = (base_url or "").rstrip("/")
    if root.endswith("/chat/completions"):
        root = root[: -len("/chat/completions")]
    if root:
        fetcher = get_json or _get_json
        try:
            data = fetcher(f"{root}/models", api_key)
            for m in data.get("data") or []:
                if isinstance(m, dict) and m.get("id") == model and isinstance(m.get("context_length"), int):
                    return m["context_length"]
        except Exception:
            pass  # 网络/解析失败走下一级
    size = _table_context_window(model)
    if size is not None:
        return size
    return default


def iter_sse_lines(response) -> Iterator[str]:
    """Yield decoded SSE data payloads; stop at [DONE]; skip comments/keepalives."""
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                return
            yield payload


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        max_retries: int = 3,
        debug: bool = False,
        use_usage: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug
        self.use_usage = use_usage

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        include_usage = self.use_usage
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
            if tools:
                payload["tools"] = tools
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
            try:
                return self._request(payload, headers, on_delta)
            except _Retryable as e:
                last_error = e
            except (requests.RequestException, OSError) as e:
                last_error = e
            except LLMError as e:
                last_error = e
                if include_usage:
                    include_usage = False  # 严格网关可能拒该字段，去掉重试一次
                    continue
                raise
            if attempt < self.max_retries - 1:
                if self.debug:
                    print(f"[llm] attempt {attempt + 1} failed: {last_error}; retrying in {2 ** attempt}s")
                time.sleep(2 ** attempt)
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    def _request(self, payload: dict, headers: dict, on_delta) -> LLMResponse:
        acc = _StreamAccumulator()
        with requests.post(
            self._url(), json=payload, headers=headers, stream=True, timeout=self.timeout
        ) as resp:
            if resp.status_code != 200:
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise _Retryable(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
                raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
            for payload_line in iter_sse_lines(resp):
                if not payload_line:
                    continue
                try:
                    chunk = json.loads(payload_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    acc.usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                acc.feed(delta)
                content = delta.get("content")
                if isinstance(content, str) and on_delta:
                    on_delta(content)
        return acc.result()
