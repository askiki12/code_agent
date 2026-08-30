import asyncio

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


def _make_app(workdir, tmp_path):
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3)
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
        assert log._lines == []
        await pilot.press("ctrl+q")


def test_app_enter_task_renders_user_and_assistant(workdir, tmp_path):
    asyncio.run(_scenario_enter_task(_make_app(workdir, tmp_path)))


def test_app_new_session_clears_log(workdir, tmp_path):
    asyncio.run(_scenario_new_session(_make_app(workdir, tmp_path)))
