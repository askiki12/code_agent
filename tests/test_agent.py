from pathlib import Path

import pytest

from code_agent.agent import MAX_CONSECUTIVE_FAILURES, AgentSession
from code_agent.llm import LLMError, LLMResponse, ToolCall, Usage


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


def test_agent_on_tool_callback(workdir):
    from code_agent.tools import ToolResult
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    seen = []
    result = session.run_task("read file", on_tool=lambda name, res: seen.append((name, res)))
    assert result.finished and result.final_text == "done"
    assert len(seen) == 1
    name, res = seen[0]
    assert name == "read_file" and isinstance(res, ToolResult) and res.ok


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


def test_agent_on_assistant_and_tool_start_callbacks(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        LLMResponse(content="planning", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    starts, tool_starts = [], []
    result = session.run_task(
        "do it",
        on_assistant_start=lambda: starts.append(1),
        on_tool_start=lambda n, a: tool_starts.append((n, a)),
    )
    assert result.finished and result.final_text == "done"
    assert len(starts) == 2
    assert tool_starts == [("read_file", {"path": "a.txt"})]


def test_context_window_budget_64k(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object(), max_context_tokens=90000, context_window=64000)
    assert s.max_context_tokens == 44800
    assert s.context_window == 64000


def test_context_window_budget_128k(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object(), max_context_tokens=90000, context_window=128000)
    assert s.max_context_tokens == 89600


def test_context_window_defaults(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object())
    assert s.max_context_tokens == 90000
    assert s.context_window == 1_000_000


def test_run_task_on_stats_heuristic_fallback(workdir):
    from code_agent.agent import AgentSession

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    got = []
    res = s.run_task("task", on_stats=got.append)
    assert res.finished is True
    assert len(got) == 1
    assert got[0].heuristic is True
    assert got[0].prompt_tokens > 0
    assert s.last_usage is got[0]


def test_run_task_on_stats_real_usage(workdir):
    from code_agent.agent import AgentSession

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=123, completion_tokens=4, cached_tokens=50))

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    got = []
    s.run_task("task", on_stats=got.append)
    assert got[0].prompt_tokens == 123
    assert got[0].cached_tokens == 50
    assert got[0].heuristic is False


def test_rename_session_creates_then_pins(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.session_id is None
    title = s.rename_session("my title")
    assert title == "my title"
    assert s.session_id is not None
    meta, _ = store.load(s.session_id)
    assert meta["title"] == "my title"
    assert meta.get("title_pinned") is True


def test_rename_session_existing_id(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("auto")
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store, session_id=sid)
    s.rename_session("new name")
    meta, _ = store.load(sid)
    assert meta["title"] == "new name"


def test_load_session_sets_heuristic_last_usage(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("t")
    store.save(sid, [{"role": "user", "content": "hello world hello world"}])
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.last_usage is None
    s.load_session(sid)
    assert s.last_usage is not None
    assert s.last_usage.heuristic is True
    assert s.last_usage.prompt_tokens > 0


def test_new_session_clears_last_usage(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=_LLM(), store=store)
    s.run_task("task")
    assert s.last_usage is not None
    s.new_session()
    assert s.last_usage is None


def test_current_title(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("my title")
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store, session_id=sid)
    assert s.current_title() == "my title"


def test_current_title_new_session(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.current_title() == ""


def test_current_title_without_store(tmp_path):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=str(tmp_path), llm=object())
    assert s.current_title() == ""


def test_run_task_tools_from_registry(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    import os as _os

    d = _os.path.join(str(tmp_path / "proj"), ".code_agent", "skills", "greeting")
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))

    class _LLM:
        def __init__(self):
            self.tools = None

        def chat(self, messages, tools=None, on_delta=None):
            self.tools = tools
            return LLMResponse(content="done", tool_calls=[])

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, skills=reg)
    s.run_task("task")
    names = {t["function"]["name"] for t in llm.tools}
    assert "read_file" in names
    assert "use_skill" in names
    assert "dispatch_subagent" in names


def test_run_task_hides_conditional_schemas(workdir):
    class _LLM:
        def __init__(self):
            self.tools = None

        def chat(self, messages, tools=None, on_delta=None):
            self.tools = tools
            return LLMResponse(content="done", tool_calls=[])

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, allow_subagent=False)
    s.run_task("task")
    names = {t["function"]["name"] for t in llm.tools}
    assert "use_skill" not in names
    assert "dispatch_subagent" not in names


def test_run_task_mixed_visible_flags(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    import os as _os

    d = _os.path.join(str(tmp_path / "proj"), ".code_agent", "skills", "greeting")
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))

    class _LLM:
        def __init__(self):
            self.tools = None

        def chat(self, messages, tools=None, on_delta=None):
            self.tools = tools
            return LLMResponse(content="done", tool_calls=[])

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, skills=reg, allow_subagent=False)
    s.run_task("task")
    names = {t["function"]["name"] for t in llm.tools}
    assert "use_skill" in names
    assert "dispatch_subagent" not in names


def test_use_skill_without_skills(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object())
    res = s._run_tool(ToolCall(id="c1", name="use_skill", arguments={"name": "x"}))
    assert res.ok is False and "skills are not available" in res.output
    assert "AttributeError" not in res.output


def test_run_tool_unknown(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    s.run_task("task")
    assert any(
        m["role"] == "tool" and "unknown tool: nonexistent" in str(m.get("content", ""))
        for m in s.conversation.messages
    )


def test_run_tool_bypass_policy(workdir):
    from code_agent.llm import ToolCall
    from code_agent.permissions import Policy

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    policy = Policy(deny=["dispatch_subagent:*", "run_command:*"])
    s = AgentSession(workdir=workdir, llm=_LLM(), policy=policy)
    tc = ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "hi"})
    res = s._run_tool(tc)
    assert res.ok is True  # 编排工具绕过 deny
    tc2 = ToolCall(id="c2", name="run_command", arguments={"command": "ls"})
    res2 = s._run_tool(tc2)
    assert res2.ok is False and "permission denied" in res2.output


def test_run_tool_dispatch_disabled_message(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object(), allow_subagent=False)
    tc = ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "x"})
    res = s._run_tool(tc)
    assert res.ok is False and "subagent dispatch is disabled" in res.output


def test_run_tool_respects_validate(workdir):
    from code_agent.llm import ToolCall
    from code_agent.tools import Tool

    class _Guarded(Tool):
        name = "guarded"
        description = ""
        parameters = {}
        required = []

        def validate(self, args):
            return "missing x" if "x" not in args else None

        def execute(self, args, workdir):
            from code_agent.tools import ToolResult
            return ToolResult(ok=True, output="ok")

    s = AgentSession(workdir=workdir, llm=object())
    s._registry.register(_Guarded())
    res = s._run_tool(ToolCall(id="c1", name="guarded", arguments={}))
    assert res.ok is False and res.output == "missing x"
    res2 = s._run_tool(ToolCall(id="c2", name="guarded", arguments={"x": 1}))
    assert res2.ok is True


def test_run_tool_validate_raise_guarded(workdir):
    from code_agent.llm import ToolCall
    from code_agent.tools import Tool, ToolResult

    class _Boom(Tool):
        name = "boom"
        description = ""
        parameters = {}
        required = []

        def validate(self, args):
            raise RuntimeError("validate broke")

        def execute(self, args, workdir):
            return ToolResult(ok=True, output="ok")

    s = AgentSession(workdir=workdir, llm=object())
    s._registry.register(_Boom())
    res = s._run_tool(ToolCall(id="c1", name="boom", arguments={}))
    assert res.ok is False
    assert "tool crashed" in res.output
    assert "validate broke" in res.output


def test_memory_default_off_no_memory_tools(workdir):
    s = AgentSession(workdir=workdir, llm=object())
    assert s._registry.get("remember") is None
    assert s._registry.get("recall") is None
    assert s._registry.get("create_skill") is None


def test_memory_tools_present_when_enabled(workdir):
    s = AgentSession(workdir=workdir, llm=object(), memory=True)
    assert s._registry.get("remember") is not None
    assert s._registry.get("recall") is not None
    assert s._registry.get("create_skill") is None
    names = {t["function"]["name"] for t in s._registry.schemas()}
    assert {"remember", "recall"} <= names
    assert "create_skill" not in names


def test_remember_recall_tools(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object(), memory=True)
    r = s._run_tool(ToolCall(id="c1", name="remember", arguments={"content": "the project uses uv"}))
    assert r.ok is True
    r2 = s._run_tool(ToolCall(id="c2", name="recall", arguments={"query": "uv"}))
    assert r2.ok is True and "uv" in r2.output


def test_create_skill_tool(workdir):
    from code_agent.llm import ToolCall
    from code_agent.skills import SkillRegistry
    reg = SkillRegistry(workdir)
    s = AgentSession(workdir=workdir, llm=object(), skills=reg, memory=True)
    r = s._run_tool(ToolCall(id="c1", name="create_skill", arguments={
        "name": "build", "description": "build", "content": "run pytest"}))
    assert r.ok is True
    assert reg.load("build") is not None


def test_remember_when_disabled(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object())
    r = s._run_tool(ToolCall(id="c1", name="remember", arguments={"content": "x"}))
    assert r.ok is False and "unknown tool" in r.output


def test_inject_memory_failure_does_not_break_run(workdir, tmp_path, monkeypatch):
    from code_agent.session import SessionStore

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True, store=store)
    s._memory.add("uv environment")

    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(s._memory, "_save", _boom)
    res = s.run_task("uv task")
    assert res.finished is True
    assert s.session_id is not None


def test_recall_top_k_bool_falls_back_to_default(workdir):
    from code_agent.llm import ToolCall

    s = AgentSession(workdir=workdir, llm=object(), memory=True)
    for m in ["uv setup guide", "uv sync env", "uv run pytest", "extra uv note"]:
        s._memory.add(m)
    res = s._run_tool(ToolCall(id="c1", name="recall", arguments={"query": "uv", "top_k": True}))
    assert res.ok is True
    assert len(res.output.strip().splitlines()) == 3


def test_memory_auto_inject(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s._memory.add("the project uses uv for the environment")
    s.run_task("set up the uv environment")
    assert any(
        "[Project memory]" in str(m.get("content", "")) for m in s.conversation.messages
        if m["role"] == "system"
    )


def test_memory_inject_once(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s._memory.add("uv environment")
    s.run_task("uv task one")
    s.run_task("uv task two")
    count = sum(1 for m in s.conversation.messages
                if "[Project memory]" in str(m.get("content", "")))
    assert count == 1


def test_memory_auto_memorize_on_success(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            last = str(messages[-1]["content"])
            if "Extract 1-3" in last:
                return LLMResponse(content='["the project builds with uv"]', tool_calls=[])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s.run_task("do something")
    assert any("the project builds with uv" in e.content for e in s._memory.all())


def test_memory_auto_memorize_skipped_on_failure(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n <= 3:
                return LLMResponse(content="", tool_calls=[ToolCall(id=f"c{self.n}", name="nonexistent", arguments={})])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True, max_iterations=5)
    res = s.run_task("boom")
    assert res.finished is False
    assert s._memory.all() == []


def test_subagent_memory_disabled(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0
            self.tools_calls = []

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            self.tools_calls.append([t["function"]["name"] for t in (tools or [])])
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub"})])
            if self.n == 2:
                return LLMResponse(content="sub done", tool_calls=[])
            return LLMResponse(content="[]", tool_calls=[])  # parent auto-summary

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, memory=True)
    s.run_task("parent task")
    sub_tools = set(llm.tools_calls[1])
    assert "dispatch_subagent" not in sub_tools
    assert not ({"remember", "recall", "create_skill"} & sub_tools)


def test_prompt_base_tool_guidance(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Choosing tools:" in system
    assert "Discover before reading" in system
    assert "edit_file" in system


def test_prompt_dispatch_guidance_present(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "When to delegate" in system
    assert "dispatch_subagent(task)" in system


def test_prompt_dispatch_guidance_absent_for_subagent(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, allow_subagent=False)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "When to delegate" not in system
    assert "You are a subagent" in system


def test_prompt_memory_guidance_present(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, memory=True)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "recall(query, top_k)" in system
    assert "remember(content, tags)" in system


def test_prompt_memory_guidance_absent(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "recall(query, top_k)" not in system


def test_prompt_skill_use_guidance(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Available skills" in system
    assert "use_skill(name)" in system


def test_prompt_skill_create_guidance_when_memory(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg, memory=True)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Authoring skills" in system
    assert "create_skill(name, description, content)" in system
    assert ".code_agent" in system


def test_prompt_skill_create_guidance_absent_without_memory(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Authoring skills" not in system


def test_prompt_guidance_matches_schema(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))

    def _run(**kwargs):
        llm = FakeLLM([LLMResponse(content="done", tool_calls=[]), LLMResponse(content="done", tool_calls=[])])
        session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, **kwargs)
        session.run_task("hi")
        names = {t["function"]["name"] for t in session._registry.schemas()}
        return llm.calls[0][0]["content"], names

    system, names = _run()
    assert "When to delegate" in system
    assert "use_skill(name)" not in system
    assert "recall(query, top_k)" not in system
    assert "create_skill" not in names
    assert "remember" not in names

    system, names = _run(skills=reg, memory=True)
    assert "Available skills" in system
    assert "use_skill(name)" in system
    assert "Authoring skills" in system
    assert "create_skill(name, description, content)" in system
    assert "recall(query, top_k)" in system
    assert {"use_skill", "remember", "recall", "create_skill"} <= names

    system, names = _run(memory=True)
    assert "recall(query, top_k)" in system
    assert "use_skill(name)" not in system
    assert "Authoring skills" not in system
    assert "create_skill" not in names
    assert {"remember", "recall"} <= names

    system, names = _run(allow_subagent=False)
    assert "When to delegate" not in system
    assert "You are a subagent" in system
