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
