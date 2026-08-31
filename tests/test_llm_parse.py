import pytest

from code_agent.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    Usage,
    _StreamAccumulator,
    iter_sse_lines,
    parse_tool_arguments,
    resolve_context_window,
)


def test_accumulate_content():
    acc = _StreamAccumulator()
    acc.feed({"content": "Hel"})
    acc.feed({"content": "lo"})
    resp = acc.result()
    assert resp.content == "Hello"
    assert resp.tool_calls == []


def test_accumulate_tool_call_fragments():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}]})
    acc.feed({"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a'}}]})
    acc.feed({"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]})
    resp = acc.result()
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "a.txt"}


def test_accumulate_multiple_tool_calls():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        {"index": 1, "id": "c2", "function": {"name": "list_dir", "arguments": "{}"}},
    ]})
    resp = acc.result()
    assert [tc.name for tc in resp.tool_calls] == ["read_file", "list_dir"]


def test_parse_tool_arguments_empty():
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("   ") == {}


def test_parse_tool_arguments_invalid():
    with pytest.raises(LLMError):
        parse_tool_arguments("{not json")


def test_parse_tool_arguments_nonobject():
    with pytest.raises(LLMError):
        parse_tool_arguments('"just a string"')


def test_result_raises_on_malformed_arguments():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{"}}]})
    with pytest.raises(LLMError):
        acc.result()


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode):
        return iter(self._lines)


def test_iter_sse_lines_stops_at_done():
    resp = _FakeResponse(["data: {\"x\": 1}", "", ": keepalive", "data: [DONE]", "data: ignored"])
    assert list(iter_sse_lines(resp)) == ['{"x": 1}']


def test_accumulator_captures_usage():
    acc = _StreamAccumulator()
    acc.feed({"content": "hi"})
    acc.feed({"usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                        "prompt_tokens_details": {"cached_tokens": 40}}})
    resp = acc.result()
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 100
    assert resp.usage.cached_tokens == 40
    assert resp.usage.heuristic is False


def test_accumulator_usage_none_when_missing():
    acc = _StreamAccumulator()
    acc.feed({"content": "x"})
    assert acc.result().usage is None


def test_accumulator_usage_cached_default_zero():
    acc = _StreamAccumulator()
    acc.feed({"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    u = acc.result().usage
    assert u.cached_tokens == 0


def test_chat_keeps_include_usage_when_accepted(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        return LLMResponse(content="ok", tool_calls=[])

    monkeypatch.setattr(client, "_request", fake_request)
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert len(calls) == 1
    assert calls[0]["stream_options"] == {"include_usage": True}


def test_chat_falls_back_without_include_usage(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        if len(calls) == 1:
            raise LLMError("HTTP 400: unknown field stream_options")
        return LLMResponse(content="ok", tool_calls=[])

    monkeypatch.setattr(client, "_request", fake_request)
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in calls[1]


def test_chat_propagates_error_when_retry_still_fails(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        raise LLMError("HTTP 401: bad key")

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2  # 首带 include_usage，回退一次仍失败


def test_resolve_context_window_from_models():
    def get_json(url, api_key):
        assert url.endswith("/models")
        return {"data": [{"id": "custom-model", "context_length": 42000}]}

    assert resolve_context_window("custom-model", "https://api.example.com/v1", "k", get_json=get_json) == 42000


def test_resolve_context_window_strips_chat_completions():
    def get_json(url, api_key):
        return {"data": [{"id": "custom", "context_length": 42000}]}

    assert resolve_context_window("custom", "https://api.example.com/v1/chat/completions", "k", get_json=get_json) == 42000


def test_resolve_context_window_table_fallback():
    assert resolve_context_window("deepseek-chat", "", "") == 64000
    assert resolve_context_window("gpt-4o-2024-08-06", "", "") == 128000


def test_resolve_context_window_default():
    assert resolve_context_window("unknown-model", "", "") == 1_000_000


def test_resolve_context_window_network_failure_silent():
    def get_json(url, api_key):
        raise RuntimeError("boom")

    assert resolve_context_window("anything", "https://x/v1", "k", get_json=get_json) == 1_000_000
