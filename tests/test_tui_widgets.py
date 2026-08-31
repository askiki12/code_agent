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
from code_agent.tui.widgets import _fmt_k, _usage_segments, StatusBar


def test_fmt_k():
    assert _fmt_k(999) == "999"
    assert _fmt_k(1000) == "1k"
    assert _fmt_k(90000) == "90k"
    assert _fmt_k(12340) == "12.3k"
    assert _fmt_k(1_000_000) == "1M"
    assert _fmt_k(1_500_000) == "1.5M"


def test_usage_segments_full():
    ctx, cache = _usage_segments(Usage(prompt_tokens=12340, cached_tokens=5000), 90000)
    assert ctx == "ctx 12.3k/90k 13%"
    assert cache == "cache 40%"


def test_usage_segments_heuristic_prefix_and_no_cache():
    ctx, cache = _usage_segments(Usage(prompt_tokens=1000, heuristic=True), 90000)
    assert ctx == "ctx ~1k/90k 1%"
    assert cache == ""


def test_usage_segments_none():
    assert _usage_segments(None, 90000) == ("", "")


def test_usage_segments_zero_cache_omitted():
    ctx, cache = _usage_segments(Usage(prompt_tokens=5000, cached_tokens=0), 90000)
    assert "cache" not in cache
    assert ctx == "ctx 5k/90k 5%"


def test_usage_segments_compact_drops_pct_and_cache():
    ctx, cache = _usage_segments(Usage(prompt_tokens=12340, cached_tokens=5000), 90000, compact=True)
    assert ctx == "ctx 12.3k/90k"
    assert cache == ""


def test_status_bar_renders_usage(monkeypatch):
    monkeypatch.setattr("code_agent.tui.widgets._status_width", lambda: 200)
    sb = StatusBar()
    sb.update_status("idle", model="m", session_id="s1",
                     workspace_line="Workspace: w", usage=Usage(prompt_tokens=12000, cached_tokens=3000),
                     context_window=90000)
    plain = sb.render().plain
    assert "ctx 12k/90k 13%" in plain
    assert "cache 25%" in plain


def test_status_bar_trims_pct_when_narrow(monkeypatch):
    monkeypatch.setattr("code_agent.tui.widgets._status_width", lambda: 30)
    sb = StatusBar()
    sb.update_status("idle", model="m", session_id="s1",
                     workspace_line="Workspace: w", usage=Usage(prompt_tokens=12000),
                     context_window=90000)
    plain = sb.render().plain
    assert "12k/90k" in plain
    assert "%" not in plain
