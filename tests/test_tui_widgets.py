import pytest

from code_agent.tui.widgets import ConversationLog, SessionList


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
