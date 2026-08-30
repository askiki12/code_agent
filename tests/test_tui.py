from code_agent.tui import format_assistant, format_tool, format_user


def test_format_user():
    assert format_user("hello") == "> user: hello"


def test_format_assistant():
    assert format_assistant("done") == "assistant: done"


def test_format_tool_ok_first_line():
    assert format_tool("read_file", True, False, "line1\nline2") == "[tool] read_file ok | line1"


def test_format_tool_failed_truncated():
    assert format_tool("run_command", False, True, "boom\nmore") == "[tool] run_command failed (truncated) | boom"


def test_format_tool_empty_output():
    assert format_tool("list_dir", True, False, "") == "[tool] list_dir ok"


def test_append_tool_line():
    from code_agent.tools import ToolResult
    from code_agent.tui import _append_tool_line
    conv = []
    _append_tool_line(conv, "read_file", ToolResult(ok=True, output="line1\nline2"))
    assert conv == ["[tool] read_file ok | line1"]


class _StubConsole:
    def __init__(self, lines):
        self._lines = list(lines)

    def input(self, prompt=""):
        return self._lines.pop(0)


class _StubLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, x):
        pass

    def refresh(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_run_tui_smoke(workdir, monkeypatch):
    from code_agent.agent import AgentSession
    from code_agent.llm import LLMResponse
    from code_agent.tui import run_tui

    class _FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.calls += 1
            if on_delta:
                on_delta("streamed")
            if self.calls == 1:
                from code_agent.llm import ToolCall
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="nonexistent_tool", arguments={})])
            return LLMResponse(content="done", tool_calls=[])

    llm = _FakeLLM()
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    monkeypatch.setattr("code_agent.tui.Live", _StubLive)
    monkeypatch.setattr("code_agent.tui.Console", lambda **kw: _StubConsole(["hello", "exit"]))
    run_tui(session, store=None, workspace=None, model="m")
    assert llm.calls == 2
