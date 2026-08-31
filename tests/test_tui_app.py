import asyncio
import time
import types
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


def _make_app(workdir, tmp_path, llm=None, skills=None):
    if llm is None:
        llm = _FakeLLM()
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3, skills=skills)
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


def test_app_skill_marker(workdir, tmp_path):
    from code_agent.llm import ToolCall
    from code_agent.skills import SkillRegistry

    d = Path(workdir, ".code_agent", "skills", "code-review")
    d.mkdir(parents=True, exist_ok=True)
    Path(d, "SKILL.md").write_text(
        "---\nname: code-review\ndescription: review code\n---\nsteps\n", encoding="utf-8"
    )

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "code-review"})])
            return LLMResponse(content="done", tool_calls=[])

    app = _make_app(workdir, tmp_path, _LLM(), skills=SkillRegistry(workdir))

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "review it"
            await pilot.press("enter")
            for _ in range(150):
                log = app.query_one("#log")
                text = "".join(l.plain for l in log._lines)
                if "[skill] ✓ code-review" in text:
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "[skill] 加载 code-review…" in text or "[skill] ✓ code-review" in text
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
            assert "[subagent] ✓ 完成" in text
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


def test_app_ctrl_p_hidden_from_footer():
    from code_agent.tui.app import CodeAgentApp
    bp = next(b for b in CodeAgentApp.BINDINGS if b.key == "ctrl+p")
    assert bp.show is False


def test_app_choose_skill_no_skills(workdir, tmp_path):
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await asyncio.sleep(0.05)
            assert len(app.screen_stack) == 1  # 未 push 弹窗
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_choose_skill_dispatches(workdir, tmp_path):
    from code_agent.llm import LLMResponse
    from code_agent.skills import SkillRegistry

    class _FakeLLM:
        def __init__(self):
            self.tasks = []

        def chat(self, messages, tools=None, on_delta=None):
            self.tasks.append(messages)
            return LLMResponse(content="done", tool_calls=[])

    proj = str(tmp_path / "proj")
    import os
    d = os.path.join(proj, ".code_agent", "skills", "greeting")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3)
    session.skills = reg
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "greet me"
            await pilot.press("ctrl+s")
            await asyncio.sleep(0.05)
            # 直接调用回调验证派发路径（弹窗内 OptionSelected→dismiss→callback
            # 闭环由 no-bubble 回归测试覆盖；Esc 关闭路径由 test_app_skill_modal_esc_dismisses 覆盖）
            app._on_skill_chosen("greeting")
            for _ in range(80):
                if session.conversation.messages and session.conversation.messages[-1]["role"] == "assistant":
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "请使用技能 greeting 完成：greet me" in text
            assert session.conversation.messages[-1]["role"] == "assistant"
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_skill_selection_option_selected_no_bubble(workdir, tmp_path, monkeypatch):
    from code_agent.llm import LLMResponse
    from code_agent.skills import SkillRegistry
    from code_agent.tui.widgets import SkillList

    class _FakeLLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    proj = str(tmp_path / "proj")
    import os
    d = os.path.join(proj, ".code_agent", "skills", "greeting")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    session.skills = reg
    app = CodeAgentApp(session, store, None, model="test")
    # 固定为「非忙」状态，让冒泡路径确定走到 load_session 分支：
    # 若不 stop 事件，技能名会被 CodeAgentApp 误当作 session id → 追加 session not found
    monkeypatch.setattr(app, "_busy", lambda: False)

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "greet me"
            await pilot.press("ctrl+s")
            await asyncio.sleep(0.05)
            sl = app.screen.query_one("#skill-list", SkillList)
            # 通过真实消息事件触发选中：OptionSelected 由 SkillScreen 处理并 stop，
            # 不得冒泡到 CodeAgentApp 被误当作 session id（回归测试）
            selected = type(sl).OptionSelected(sl, sl._options[0], 0)
            sl.post_message(selected)
            await asyncio.sleep(0.05)
            for _ in range(100):
                log = app.screen.query_one("#log")
                text = "".join(l.plain for l in log._lines)
                if "assistant" in text and "> user: 请使用技能 greeting" in text:
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.screen.query_one("#log")._lines)
            assert "session not found" not in text
            assert "请使用技能 greeting 完成：greet me" in text
            assert session.conversation.messages[-1]["role"] == "assistant"
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_skill_modal_esc_dismisses(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    import os as _os

    d = _os.path.join(str(tmp_path / "proj"), ".code_agent", "skills", "greeting")
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3)
    session.skills = reg
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+s")
            await asyncio.sleep(0.05)
            assert len(app.screen_stack) == 2  # 弹窗已打开
            await pilot.press("escape")
            await asyncio.sleep(0.05)
            assert len(app.screen_stack) == 1  # 已关闭
            log = app.query_one("#log")
            assert log._lines == []  # 无任务派发
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_rename_session(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            inp.value = "我的会话"
            await pilot.press("enter")
            assert session.session_id is not None
            meta, _ = store.load(session.session_id)
            assert meta["title"] == "我的会话"
            assert meta.get("title_pinned") is True
            assert inp._rename_mode is False
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_rename_esc_cancels(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            await pilot.press("escape")
            assert inp._rename_mode is False
            assert session.session_id is None
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_rename_disarmed_on_new_session(workdir, tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            await pilot.press("ctrl+n")  # 切换新会话应复位 rename 模式
            assert inp._rename_mode is False
            inp.value = "改名"
            await pilot.press("enter")
            for _ in range(50):
                log = app.query_one("#log")
                if "答复内容" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "> user: 改名" in text  # 作为普通任务执行，而非创建/重命名会话
            assert "答复内容" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_rename_disarmed_on_session_select(workdir, tmp_path):
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("旧标题")
    store.rename(sid, "旧标题")  # 固定标题，避免任务持久化自动改标题干扰断言
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            app.on_option_list_option_selected(
                types.SimpleNamespace(option=types.SimpleNamespace(id=sid))
            )
            assert inp._rename_mode is False  # 选中会话应复位 rename 模式
            assert session.session_id == sid
            inp.value = "覆盖标题"
            await pilot.press("enter")
            for _ in range(50):
                log = app.query_one("#log")
                if "答复内容" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            meta, _ = store.load(sid)
            assert meta["title"] == "旧标题"  # 已加载会话的标题未被 rename 覆盖
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_on_stats_updates_status(workdir, tmp_path):
    from code_agent.llm import Usage
    from code_agent.tui.widgets import StatusBar

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=12000, cached_tokens=3000))

    session = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=3, context_window=90000)
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hi"
            await pilot.press("enter")
            for _ in range(80):
                sb = app.query_one("#status", StatusBar)
                if "ctx" in sb.render().plain:
                    break
                await asyncio.sleep(0.02)
            plain = sb.render().plain
            assert "ctx 12k/90k 13%" in plain
            assert "cache 25%" in plain
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
