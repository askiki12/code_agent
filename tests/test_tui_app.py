import asyncio
import time
from pathlib import Path

import pytest

from code_agent.agent import AgentSession
from code_agent.llm import LLMResponse
from code_agent.session import SessionStore
from code_agent.tui.app import CodeAgentApp


class _FakeLLM:
    def chat(self, messages, tools=None, on_delta=None):
        if on_delta:
            on_delta("答复内容")
        return LLMResponse(content="答复内容", tool_calls=[])


def _make_app(workdir, tmp_path, llm=None):
    if llm is None:
        llm = _FakeLLM()
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3)
    store = SessionStore(str(tmp_path / "sessions"))
    return CodeAgentApp(session, store, None, model="test")


async def _scenario_enter_task(app):
    async with app.run_test() as pilot:
        inp = app.query_one("#input")
        inp.value = "hello"
        await pilot.press("enter")
        for _ in range(50):  # 轮询等待 worker 完成
            log = app.query_one("#log")
            text = "".join(l.plain for l in log._lines)
            if "答复内容" in text:
                break
            await asyncio.sleep(0.02)
        text = "".join(l.plain for l in log._lines)
        assert "> user: hello" in text
        assert "答复内容" in text
        await pilot.press("ctrl+q")


async def _scenario_new_session(app):
    async with app.run_test() as pilot:
        inp = app.query_one("#input")
        inp.value = "hi"
        await pilot.press("enter")
        for _ in range(50):
            log = app.query_one("#log")
            if "答复内容" in log._lines[-1].plain:
                break
            await asyncio.sleep(0.02)
        await pilot.press("ctrl+n")
        log = app.query_one("#log")
        assert len(log._lines) == 1 and log._lines[0].plain == "New session started."
        await pilot.press("ctrl+q")


def test_app_enter_task_renders_user_and_assistant(workdir, tmp_path):
    asyncio.run(_scenario_enter_task(_make_app(workdir, tmp_path)))


def test_app_new_session_clears_log(workdir, tmp_path):
    asyncio.run(_scenario_new_session(_make_app(workdir, tmp_path)))


class _SlowLLM:
    def chat(self, messages, tools=None, on_delta=None):
        if on_delta:
            on_delta("答复内")
            time.sleep(0.25)
            on_delta("容")
        return LLMResponse(content="答复内容", tool_calls=[])


def test_app_ctrl_n_during_run_is_ignored(workdir, tmp_path):
    session = AgentSession(workdir=workdir, llm=_SlowLLM(), max_iterations=3)
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hello"
            await pilot.press("enter")
            await asyncio.sleep(0.03)  # 让首个 delta 到达
            await pilot.press("ctrl+n")  # 运行中应被拒绝，不清空
            for _ in range(150):
                log = app.query_one("#log")
                if "答复内容" in log._lines[-1].plain:
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in log._lines)
            assert "worker crash" not in text
            assert "答复内容" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_multi_round_order(workdir, tmp_path):
    from code_agent.llm import ToolCall

    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                if on_delta:
                    on_delta("思考")
                return LLMResponse(content="思考", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})])
            if on_delta:
                on_delta("结论")
            return LLMResponse(content="结论", tool_calls=[])

    app = _make_app(workdir, tmp_path, _LLM())

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "任务"
            await pilot.press("enter")
            for _ in range(150):
                log = app.query_one("#log")
                if "结论" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            lines = [l.plain for l in app.query_one("#log")._lines]
            assert lines[0].startswith("> user:")
            assert lines[1].startswith("assistant:") and "思考" in lines[1]
            assert lines[2].startswith("[tool] read_file")
            assert lines[3].startswith("assistant:") and "结论" in lines[3]
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_bang_command(workdir, tmp_path, monkeypatch):
    from code_agent.tui import app as tui_app

    class _R:
        returncode = 0
        stdout = "hello world"
        stderr = ""

    monkeypatch.setattr(tui_app.subprocess, "run", lambda *a, **k: _R())
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "!echo hi"
            await pilot.press("enter")
            for _ in range(80):
                log = app.query_one("#log")
                if "hello world" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "$ echo hi" in text
            assert "hello world" in text
            assert "[exit 0]" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_bang_timeout(workdir, tmp_path, monkeypatch):
    import subprocess as sp
    from code_agent.tui import app as tui_app

    def _boom(*a, **k):
        raise sp.TimeoutExpired("cmd", 120)

    monkeypatch.setattr(tui_app.subprocess, "run", _boom)
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "!sleep 999"
            await pilot.press("enter")
            for _ in range(80):
                log = app.query_one("#log")
                if "timed out" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "[command timed out after 120s]" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_subagent_status_marker(workdir, tmp_path):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(
                    content="", tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub"})],
                )
            return LLMResponse(content="done", tool_calls=[])

    app = _make_app(workdir, tmp_path, _LLM())

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hi"
            await pilot.press("enter")
            for _ in range(150):
                log = app.query_one("#log")
                text = "".join(l.plain for l in log._lines)
                if "[subagent] ✓ 完成" in text:
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "[subagent] 子智能体运行中…" in text or "[subagent] ✓ 完成" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_commands_and_bindings():
    from code_agent.tui.app import CodeAgentApp
    assert CodeAgentApp.COMMANDS == ()
    keys = [b.key for b in CodeAgentApp.BINDINGS]
    assert "ctrl+p" in keys and "ctrl+q" in keys and "ctrl+n" in keys and "ctrl+l" in keys


def test_app_toggle_sessions_no_error(workdir, tmp_path):
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+l")
            await pilot.press("ctrl+l")
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_command_palette_disabled(workdir, tmp_path):
    from textual.app import CommandPalette

    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            assert not app.use_command_palette
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert not CommandPalette.is_open(app)
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
