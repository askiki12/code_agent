from pathlib import Path

import pytest

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


def test_agent_with_store_saves_after_task(workdir, tmp_path):
    from code_agent.session import SessionStore
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    store = SessionStore(str(tmp_path / "sessions"))
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store)
    result = session.run_task("read the file")
    assert result.finished
    assert session.session_id is not None
    meta, msgs = store.load(session.session_id)
    assert meta["title"] == "read the file"
    assert any(m.get("role") == "tool" for m in msgs)


def test_agent_resume_restores_conversation(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("resume-me")
    store.save(sid, [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old reply"},
    ])
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store, session_id=sid, resume=True)
    assert session.session_id == sid
    result = session.run_task("continue")
    assert result.finished
    contents = [m.get("content") for m in session.conversation.messages]
    assert "old task" in contents
    assert "continue" in contents


def test_agent_resume_without_session_id_raises(workdir):
    from code_agent.session import SessionStore
    store = SessionStore("/tmp/nonexistent-sessions")
    with pytest.raises(ValueError):
        AgentSession(workdir=workdir, llm=FakeLLM([]), store=store, resume=True)


class _FailingStore:
    def __init__(self):
        self.session_id = "code_agent-fail"

    def create(self, title):
        self.session_id = "code_agent-fail"
        return self.session_id

    def save(self, session_id, messages, title=None):
        raise OSError("disk full")

    def load(self, session_id):
        raise KeyError(session_id)


def test_agent_save_failure_does_not_crash(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=_FailingStore())
    result = session.run_task("read the file")
    assert result.finished and result.final_text == "done"


def test_agent_load_session_switches_conversation(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("other")
    store.save(sid, [{"role": "user", "content": "other history"}])
    llm = FakeLLM([])
    session = AgentSession(workdir=workdir, llm=llm, store=store)
    session.load_session(sid)
    assert session.session_id == sid
    contents = [m.get("content") for m in session.conversation.messages]
    assert "other history" in contents
    assert session.conversation.is_valid()


def test_agent_with_workspace_touches_session(workdir, tmp_path):
    from code_agent.session import SessionStore
    from code_agent.workspace import Workspace
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    workspace = Workspace(str(tmp_path))
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store, workspace=workspace)
    result = session.run_task("read the file")
    assert result.finished
    assert workspace.last_session_id == session.session_id


def test_agent_without_workspace_unchanged(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("read the file")
    assert result.finished and result.final_text == "done"


def test_agent_deny_rule_blocks_tool(workdir):
    from code_agent.permissions import Policy
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, policy=Policy(deny=["read_file:*"]))
    result = session.run_task("read the file")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "permission denied" in tool_msgs[0]["content"]


def test_agent_doom_loop_blocks_repeated_call(workdir):
    from code_agent.permissions import Policy
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    call = _read_call("c1", "a.txt")
    llm = FakeLLM([call, call, call])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3, policy=Policy())
    result = session.run_task("loop")
    assert not result.finished and result.reason == "max_iterations"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert any("doom_loop" in m["content"] for m in tool_msgs)
