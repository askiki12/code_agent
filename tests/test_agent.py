from pathlib import Path

from code_agent.agent import AgentSession
from code_agent.llm import LLMError, LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, on_delta=None):
        self.calls.append(messages)
        return self.responses.pop(0)


class _RaisingLLM(FakeLLM):
    def __init__(self, responses, raise_on):
        super().__init__(responses)
        self.raise_on = raise_on

    def chat(self, messages, tools=None, on_delta=None):
        self.calls.append(messages)
        if self.raise_on is not None:
            if self.raise_on <= 0:
                return self.responses.pop(0)
            self.raise_on -= 1
        raise LLMError("simulated llm error")


def _read_call(pid, path):
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=pid, name="read_file", arguments={"path": path})]
    )


def test_agent_completes_after_tool_round(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done: hello", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("read the file")
    assert result.finished and result.reason == "complete"
    assert result.final_text == "done: hello"
    assert result.iterations == 2
    assert session.conversation.is_valid()


def test_agent_max_iterations(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    call = _read_call("c1", "a.txt")
    llm = FakeLLM([call, call, call])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3)
    result = session.run_task("loop")
    assert not result.finished and result.reason == "max_iterations"
    assert result.iterations == 3


def test_agent_stops_on_consecutive_failures(workdir):
    bad = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="nonexistent_tool", arguments={})],
    )
    llm = FakeLLM([bad, bad, bad])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10)
    result = session.run_task("boom")
    assert not result.finished
    assert result.reason == "too many consecutive tool failures"
    assert result.iterations == 3


def test_agent_recovers_after_failure(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "missing.txt"),
        _read_call("c2", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("recover")
    assert result.finished and result.final_text == "done"
    assert result.iterations == 3


def test_agent_stops_cleanly_on_persistent_llm_error(workdir):
    llm = _RaisingLLM(responses=[], raise_on=None)
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10)
    result = session.run_task("boom")
    assert not result.finished
    assert result.reason.startswith("llm error")
    assert result.iterations == 3


def test_agent_recovers_after_transient_llm_error(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = _RaisingLLM(
        responses=[
            _read_call("c1", "a.txt"),
            LLMResponse(content="done", tool_calls=[]),
        ],
        raise_on=1,
    )
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("recover")
    assert result.finished and result.reason == "complete"
    assert result.final_text == "done"
    assert result.iterations == 3
    assert session.conversation.is_valid()
