import threading
import time

import pytest

from code_agent.llm import LLMResponse, ToolCall
from code_agent.tui.worker import AgentWorker


class _FakeLLM:
    def __init__(self):
        self.chunks = ["Hel", "lo"]
        self.calls = 0

    def chat(self, messages, tools=None, on_delta=None):
        self.calls += 1
        for c in self.chunks:
            if on_delta:
                on_delta(c)
        return LLMResponse(content="hello", tool_calls=[])


class _FakeApp:
    def __init__(self):
        self.ui_ops = []

    def call_from_thread(self, fn):
        fn()
        self.ui_ops.append(fn.__name__)


def _make_session(workdir, llm):
    from code_agent.agent import AgentSession
    return AgentSession(workdir=workdir, llm=llm, max_iterations=3)


def test_worker_streams_and_done(workdir):
    llm = _FakeLLM()
    session = _make_session(workdir, llm)
    app = _FakeApp()
    deltas, tools, done = [], [], []

    w = AgentWorker(
        app, session,
        on_delta=lambda c: deltas.append(c),
        on_tool=lambda n, r: tools.append((n, r)),
        on_done=lambda r: done.append(r),
    )
    w.start("hi")
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.02)
    assert done and done[0].final_text == "hello"
    assert "".join(deltas) == "Hel" + "lo"
    assert app.ui_ops  # 桥被调用


def test_worker_bridges_new_callbacks(workdir):
    from code_agent.llm import ToolCall
    from code_agent.tui.worker import AgentWorker

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})])
            return LLMResponse(content="done", tool_calls=[])

    from pathlib import Path
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    session = _make_session(workdir, _LLM())
    app = _FakeApp()
    starts, tool_starts = [], []

    w = AgentWorker(
        app, session,
        on_delta=lambda c: None,
        on_tool=lambda n, r: None,
        on_done=lambda r: None,
        on_assistant_start=lambda: starts.append(1),
        on_tool_start=lambda n, a: tool_starts.append((n, a)),
    )
    w.start("hi")
    deadline = time.time() + 5
    while len(starts) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(starts) == 2
    assert tool_starts == [("read_file", {"path": "a.txt"})]
