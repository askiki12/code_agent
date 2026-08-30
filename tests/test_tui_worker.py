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
