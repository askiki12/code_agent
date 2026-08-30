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


from code_agent.tui import _line_style


def test_line_style_user():
    assert _line_style("> user: hi") == "bold cyan"


def test_line_style_assistant_default():
    assert _line_style("assistant: hi") == "default"


def test_line_style_tool_ok_and_failed():
    assert _line_style("[tool] read_file ok | a") == "dim"
    assert _line_style("[tool] run_command failed (truncated) | boom") == "dim red"


def test_line_style_stopped_and_session():
    assert _line_style("[agent] stopped: max_iterations") == "yellow"
    assert _line_style("[session code_agent-123]") == "magenta"


def test_line_style_other_dim():
    assert _line_style("1. something") == "dim"
