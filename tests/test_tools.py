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
