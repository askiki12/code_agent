from pathlib import Path

import pytest

from code_agent.agent import MAX_CONSECUTIVE_FAILURES, AgentSession
from code_agent.llm import LLMError, LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.tools_calls = []

    def chat(self, messages, tools=None, on_delta=None):
        self.calls.append(messages)
        self.tools_calls.append(tools)
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


def _write_skill(root, name, desc, body):
    import os
    d = os.path.join(root, ".code_agent", "skills", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")


def test_agent_with_skills_injects_prompt(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Available skills" in system and "code-review" in system
    assert llm.tools_calls[0] is not None and any(
        tc["function"]["name"] == "use_skill" for tc in llm.tools_calls[0]
    )


def test_agent_use_skill_loads_content(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "review steps here")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    skill_call = LLMResponse(
        content="", tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "code-review"})]
    )
    llm = FakeLLM([skill_call, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, skills=reg)
    result = session.run_task("review the code")
    assert result.finished
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "review steps here" in tool_msgs[0]["content"]


def test_agent_use_skill_not_found(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    skill_call = LLMResponse(
        content="", tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "nope"})]
    )
    llm = FakeLLM([skill_call, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, skills=reg)
    result = session.run_task("do skill")
    assert result.finished
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "skill not found" in tool_msgs[0]["content"]


def test_agent_no_skills_no_skill_tool(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    assert all(
        t is None or not any(tc["function"]["name"] == "use_skill" for tc in t)
        for t in llm.tools_calls
    )


def test_agent_use_skill_non_dict_arguments(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    skill_call = LLMResponse(
        content="", tool_calls=[ToolCall(id="c1", name="use_skill", arguments="oops")]
    )
    llm = FakeLLM([skill_call, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, skills=reg)
    result = session.run_task("do skill")
    assert result.finished
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "skill arguments must be an object" in tool_msgs[0]["content"]


def test_agent_main_has_dispatch_tool(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    assert llm.tools_calls[0] is not None and any(
        tc["function"]["name"] == "dispatch_subagent" for tc in llm.tools_calls[0]
    )


def test_agent_subagent_tools_exclude_dispatch(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, allow_subagent=False)
    session.run_task("hi")
    tools = [tc["function"]["name"] for tc in (llm.tools_calls[0] or [])]
    assert "dispatch_subagent" not in tools
    assert "read_file" in tools
    system = llm.calls[0][0]["content"]
    assert "You are a subagent" in system


def test_agent_subagent_dispatch_runtime_denied(workdir):
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "x"})],
    )
    llm = FakeLLM([dispatch, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, allow_subagent=False)
    result = session.run_task("hi")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "subagent dispatch is disabled" in tool_msgs[0]["content"]


def test_agent_dispatch_subagent_success(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub task"})],
    )
    llm = FakeLLM([
        dispatch,
        _read_call("s1", "a.txt"),
        LLMResponse(content="sub report", tool_calls=[]),
        LLMResponse(content="done main", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10)
    result = session.run_task("main task")
    assert result.finished and result.final_text == "done main"
    parent_tool = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert parent_tool and "sub report" in parent_tool[0]["content"]
    sub_first = llm.calls[1]
    assert sub_first[0]["role"] == "system" and "You are a subagent" in sub_first[0]["content"]
    assert sub_first[1] == {"role": "user", "content": "sub task"}
    assert "main task" not in str(sub_first)
    sub_tools = [tc["function"]["name"] for tc in (llm.tools_calls[1] or [])]
    assert "dispatch_subagent" not in sub_tools
    assert session.conversation.is_valid()


def test_agent_dispatch_missing_task(workdir):
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={})],
    )
    llm = FakeLLM([dispatch, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("hi")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "task is required" in tool_msgs[0]["content"]


def test_agent_dispatch_non_dict_arguments(workdir):
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments="oops")],
    )
    llm = FakeLLM([dispatch, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("hi")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "task is required" in tool_msgs[0]["content"]


def test_agent_dispatch_blank_task(workdir):
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "   "})],
    )
    llm = FakeLLM([dispatch, LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("hi")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "task is required" in tool_msgs[0]["content"]


def test_agent_dispatch_subagent_not_finished(workdir):
    bad = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="x1", name="nonexistent_tool", arguments={})],
    )
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub task"})],
    )
    responses = [dispatch] + [bad] * MAX_CONSECUTIVE_FAILURES + [LLMResponse(content="done", tool_calls=[])]
    llm = FakeLLM(responses)
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10)
    result = session.run_task("main task")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "(subagent returned no report; status:" in tool_msgs[0]["content"]


def test_agent_dispatch_inherits_policy(workdir):
    from code_agent.permissions import Policy
    run = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="s1", name="run_command", arguments={"command": "echo hi"})],
    )
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub task"})],
    )
    llm = FakeLLM([dispatch, run, LLMResponse(content="sub done", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10, policy=Policy(deny=["run_command:*"]))
    result = session.run_task("main task")
    assert result.finished and result.final_text == "done"
    sub_second = llm.calls[2]
    sub_tool = [m for m in sub_second if m["role"] == "tool"]
    assert sub_tool and "permission denied" in sub_tool[0]["content"]


def test_agent_dispatch_does_not_persist_sub_session(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub task"})],
    )
    llm = FakeLLM([dispatch, LLMResponse(content="sub report", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store)
    result = session.run_task("main task")
    assert result.finished and result.final_text == "done"
    assert len(store.list_sessions()) == 1


def test_agent_dispatch_passes_ask_to_sub(workdir):
    from code_agent.permissions import Policy
    ask_calls = []

    def fake_ask(prompt):
        ask_calls.append(prompt)
        return "y"

    run = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="s1", name="run_command", arguments={"command": "echo hi"})],
    )
    dispatch = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub task"})],
    )
    llm = FakeLLM([dispatch, run, LLMResponse(content="sub done", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(
        workdir=workdir, llm=llm, max_iterations=10,
        policy=Policy(ask=["run_command:*"]), interact=True, ask=fake_ask,
    )
    result = session.run_task("main task")
    assert result.finished and result.final_text == "done"
    assert ask_calls and "[permission]" in ask_calls[0]
