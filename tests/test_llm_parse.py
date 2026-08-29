import pytest

from code_agent.llm import (
    LLMError,
    _StreamAccumulator,
    iter_sse_lines,
    parse_tool_arguments,
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
