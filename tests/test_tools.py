import os
from pathlib import Path

from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute, truncate


def _write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def test_truncate_short():
    text, truncated = truncate("hello", limit=10)
    assert text == "hello"
    assert truncated is False


def test_truncate_long():
    text = "x" * 100
    out, truncated = truncate(text, limit=10)
    assert truncated is True
    assert len(out) < 100
    assert "TRUNCATED" in out


def test_read_file_success(workdir):
    _write(os.path.join(workdir, "a.txt"), "line1\nline2\n")
    r = execute("read_file", {"path": "a.txt"}, workdir)
    assert r.ok and "line1" in r.output and "line2" in r.output


def test_read_file_with_range(workdir):
    _write(os.path.join(workdir, "a.txt"), "\n".join(f"line{i}" for i in range(1, 6)) + "\n")
    r = execute("read_file", {"path": "a.txt", "offset": 2, "limit": 2}, workdir)
    assert r.ok and "line2" in r.output and "line3" in r.output
    assert "line1" not in r.output


def test_read_file_not_found(workdir):
    r = execute("read_file", {"path": "nope.txt"}, workdir)
    assert not r.ok and "not found" in r.output


def test_read_file_protected(workdir):
    _write(os.path.join(workdir, ".env"), "KEY=value\n")
    r = execute("read_file", {"path": ".env"}, workdir)
    assert not r.ok and "protected" in r.output


def test_list_dir(workdir):
    _write(os.path.join(workdir, "a.txt"), "x")
    _write(os.path.join(workdir, "sub", "b.txt"), "y")
    r = execute("list_dir", {}, workdir)
    assert r.ok and "a.txt" in r.output and "sub/" in r.output


def test_list_dir_skips_git(workdir):
    _write(os.path.join(workdir, ".git", "config"), "x")
    r = execute("list_dir", {}, workdir)
    assert r.ok and ".git" not in r.output


def test_list_dir_not_found(workdir):
    r = execute("list_dir", {"path": "nope"}, workdir)
    assert not r.ok and "not found" in r.output


def test_execute_unknown_tool(workdir):
    r = execute("nope", {}, workdir)
    assert not r.ok and "unknown tool" in r.output


def test_tool_schemas_have_expected_names():
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert names == {"read_file", "write_file", "edit_file", "list_dir", "run_command"}


def test_write_file(workdir):
    r = execute("write_file", {"path": "b.txt", "content": "hello world"}, workdir)
    assert r.ok
    assert Path(workdir, "b.txt").read_text(encoding="utf-8") == "hello world"


def test_write_file_creates_parent(workdir):
    r = execute("write_file", {"path": "sub/dir/c.txt", "content": "x"}, workdir)
    assert r.ok
    assert Path(workdir, "sub/dir/c.txt").read_text(encoding="utf-8") == "x"


def test_write_file_protected(workdir):
    r = execute("write_file", {"path": ".env", "content": "KEY=v"}, workdir)
    assert not r.ok and "protected" in r.output


def test_write_file_outside_workdir(workdir):
    r = execute("write_file", {"path": "../escaped.txt", "content": "x"}, workdir)
    assert not r.ok and "outside workdir" in r.output


def test_edit_file_success(workdir):
    _write(os.path.join(workdir, "d.txt"), "aaa\nbbb\n")
    r = execute("edit_file", {"path": "d.txt", "old_string": "aaa", "new_string": "AAA"}, workdir)
    assert r.ok
    assert Path(workdir, "d.txt").read_text(encoding="utf-8") == "AAA\nbbb\n"


def test_edit_file_old_not_found(workdir):
    _write(os.path.join(workdir, "d.txt"), "aaa\n")
    r = execute("edit_file", {"path": "d.txt", "old_string": "zzz", "new_string": "x"}, workdir)
    assert not r.ok and "not found" in r.output


def test_edit_file_multiple_matches(workdir):
    _write(os.path.join(workdir, "d.txt"), "aaa\naaa\n")
    r = execute("edit_file", {"path": "d.txt", "old_string": "aaa", "new_string": "X"}, workdir)
    assert not r.ok and "2 times" in r.output
    r = execute("edit_file", {"path": "d.txt", "old_string": "aaa", "new_string": "X", "replace_all": True}, workdir)
    assert r.ok
    assert Path(workdir, "d.txt").read_text(encoding="utf-8") == "X\nX\n"


def test_edit_file_protected(workdir):
    r = execute("edit_file", {"path": ".env", "old_string": "a", "new_string": "b"}, workdir)
    assert not r.ok and "protected" in r.output


def test_run_command_success(workdir):
    r = execute("run_command", {"command": "echo hi"}, workdir)
    assert r.ok and r.exit_code == 0 and "hi" in r.output


def test_run_command_failure(workdir):
    r = execute("run_command", {"command": "exit 3"}, workdir)
    assert not r.ok and r.exit_code == 3


def test_run_command_timeout(workdir):
    r = execute("run_command", {"command": "sleep 2", "timeout": 0.1}, workdir)
    assert not r.ok and "timed out" in r.output


def test_run_command_output_truncated(workdir):
    r = execute("run_command", {"command": "python3 -c \"print('x' * 20000)\""}, workdir)
    assert r.ok and r.truncated
