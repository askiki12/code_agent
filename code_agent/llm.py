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
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]


@dataclass
class _StreamAccumulator:
    content: str = ""
    _calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def feed(self, delta: dict) -> None:
        if isinstance(delta.get("content"), str):
            self.content += delta["content"]
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
        return LLMResponse(content=self.content, tool_calls=calls)


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug

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
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self._request(payload, headers, on_delta)
            except _Retryable as e:
                last_error = e
            except (requests.RequestException, OSError) as e:
                last_error = e
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
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                acc.feed(delta)
                content = delta.get("content")
                if isinstance(content, str) and on_delta:
                    on_delta(content)
        return acc.result()
