import asyncio

import pytest
from textual.app import App

from code_agent.tui.widgets import ConversationLog, PromptInput, SessionList, SkillList


def test_conversation_log_append_and_update_last():
    log = ConversationLog()
    log.append("> user: hi")
    log.append("assistant: ")
    assert log._lines[0].plain == "> user: hi"
    log.update_last("assistant: hello")
    assert log._lines[-1].plain == "assistant: hello"
    assert len(log._lines) == 2


def test_conversation_log_clear():
    log = ConversationLog()
    log.append("x")
    log.clear()
    assert log._lines == []


def test_conversation_log_update_line():
    log = ConversationLog()
    log.append("assistant: ")
    log.append("[tool] read_file ok | a")
    log.update_line(0, "assistant: hello")
    assert log._lines[0].plain == "assistant: hello"
    assert log._lines[1].plain == "[tool] read_file ok | a"


def test_conversation_log_line_styles():
    log = ConversationLog()
    log.append("> user: hi")
    log.append("[tool] read_file ok | a")
    log.append("[agent] stopped: boom")
    styles = [line.style for line in log._lines]
    assert styles == ["bold cyan", "dim", "yellow"]


def test_session_list_refresh(tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    store.create("t1")
    sl = SessionList()
    sl.refresh_from(store)
    assert sl.option_count == 1


class _PromptInputApp(App):
    def compose(self):
        yield PromptInput(id="prompt-input")


def _run_with_input(scenario) -> None:
    async def run():
        app = _PromptInputApp()
        async with app.run_test():
            inp = app.query_one("#prompt-input", PromptInput)
            await scenario(inp)

    asyncio.run(run())


def test_prompt_input_command_mode():
    async def scenario(inp):
        inp._ask_mode = False
        inp.value = "!ls"
        inp.on_input_changed(None)
        assert inp.has_class("command-mode")
        assert "shell:" in inp.placeholder
        inp.value = "ls"
        inp.on_input_changed(None)
        assert not inp.has_class("command-mode")
        assert "shell:" not in inp.placeholder

    _run_with_input(scenario)


def test_prompt_input_ask_mode_untouched():
    async def scenario(inp):
        inp.set_ask_mode("[permission] allow? [y/N] ")
        inp._ask_mode = True
        inp.value = "y"
        inp.on_input_changed(None)
        assert not inp.has_class("command-mode")
        assert "permission" in inp.placeholder

    _run_with_input(scenario)


def test_skill_list_refresh(tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    import os
    d = os.path.join(proj, ".code_agent", "skills", "greeting")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    sl = SkillList()
    sl.refresh_from(reg)
    assert sl.option_count == 1


from code_agent.llm import Usage
from code_agent.tui.widgets import StatusBar, StatusFooter, _fmt_ctx, _footer_stats


def test_fmt_ctx():
    assert _fmt_ctx(0) == "0.0k"
    assert _fmt_ctx(90000) == "90.0k"
    assert _fmt_ctx(213000) == "213.0k"
    assert _fmt_ctx(12340) == "12.3k"


def test_footer_stats_full():
    assert _footer_stats(Usage(prompt_tokens=213000, cached_tokens=90000), 1_000_000) == "213.0k(21%) cache:42%"


def test_footer_stats_no_cache():
    assert _footer_stats(Usage(prompt_tokens=213000), 1_000_000) == "213.0k(21%)"


def test_footer_stats_none():
    assert _footer_stats(None, 1_000_000) == ""


def test_footer_stats_heuristic():
    assert _footer_stats(Usage(prompt_tokens=12340, heuristic=True), 1_000_000) == "~12.3k(1%)"


def test_status_bar_workspace_path_and_session_title():
    sb = StatusBar()
    sb.update_status("idle", model="m", session_title="我的会话", workspace_line="Workspace: /home/kiki/proj")
    plain = sb.render().plain
    assert "Workspace: /home/kiki/proj" in plain
    assert "session: 我的会话" in plain


def test_status_bar_new_session_title():
    sb = StatusBar()
    sb.update_status("idle", session_title="")
    assert "session: new" in sb.render().plain


def test_status_bar_no_ctx_segment():
    sb = StatusBar()
    sb.update_status("idle", model="m", session_title="s")
    plain = sb.render().plain
    assert "ctx" not in plain and "cache" not in plain and "%" not in plain


class _FooterApp(App):
    def compose(self):
        yield StatusFooter(id="footer")


def _run_footer(scenario) -> None:
    async def run():
        app = _FooterApp()
        async with app.run_test():
            footer = app.query_one("#footer", StatusFooter)
            await scenario(footer)

    asyncio.run(run())


def test_status_footer_update_stats():
    async def scenario(footer):
        footer.update_stats("213.0k(21%) cache:40%")
        await asyncio.sleep(0.01)
        assert footer.query_one("#footer-stats").render().plain == "213.0k(21%) cache:40%"

    _run_footer(scenario)


def test_status_footer_empty_stats():
    async def scenario(footer):
        footer.update_stats("")
        await asyncio.sleep(0.01)
        assert footer.query_one("#footer-stats").render().plain == ""

    _run_footer(scenario)
