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
    assert names == {"read_file", "write_file", "edit_file", "list_dir", "run_command", "glob", "grep"}


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


def test_glob_simple(workdir):
    _write(os.path.join(workdir, "a.py"), "x")
    _write(os.path.join(workdir, "b.txt"), "x")
    r = execute("glob", {"pattern": "*.py"}, workdir)
    assert r.ok and r.output == "a.py"


def test_glob_recursive(workdir):
    _write(os.path.join(workdir, "pkg", "__init__.py"), "x")
    _write(os.path.join(workdir, "top.py"), "x")
    r = execute("glob", {"pattern": "**/*.py"}, workdir)
    assert r.ok and "top.py" in r.output and "pkg/__init__.py" in r.output


def test_glob_subdir(workdir):
    _write(os.path.join(workdir, "sub", "a.py"), "x")
    r = execute("glob", {"pattern": "*.py", "path": "sub"}, workdir)
    assert r.ok and r.output == "sub/a.py"


def test_glob_skips_protected(workdir):
    _write(os.path.join(workdir, "a.py"), "x")
    _write(os.path.join(workdir, ".env"), "K=1")
    _write(os.path.join(workdir, ".git", "config"), "x")
    r = execute("glob", {"pattern": "**/.env*"}, workdir)
    assert r.ok and r.output == "(no matches)"
    r = execute("glob", {"pattern": "**/.git/**"}, workdir)
    assert r.ok and r.output == "(no matches)" and "a.py" not in r.output


def test_glob_skips_symlinked_dir(workdir):
    real = os.path.join(workdir, "real")
    _write(os.path.join(real, "a.py"), "x")
    os.symlink(real, os.path.join(workdir, "link"))
    r = execute("glob", {"pattern": "**/*.py"}, workdir)
    assert r.ok and "real/a.py" in r.output and "link" not in r.output


def test_glob_no_match(workdir):
    r = execute("glob", {"pattern": "*.xyz"}, workdir)
    assert r.ok and "no matches" in r.output


def test_glob_requires_pattern(workdir):
    r = execute("glob", {}, workdir)
    assert not r.ok and "pattern" in r.output


def test_glob_output_truncated(workdir):
    name = "f" * 90
    for i in range(120):
        _write(os.path.join(workdir, f"{name}{i:03d}.txt"), "x")
    r = execute("glob", {"pattern": "*.txt"}, workdir)
    assert r.ok and r.truncated and "TRUNCATED" in r.output


def test_grep_content(workdir):
    _write(os.path.join(workdir, "a.py"), "alpha\nbeta\n")
    r = execute("grep", {"pattern": "beta"}, workdir)
    assert r.ok and "a.py:2:beta" in r.output


def test_grep_files_with_matches(workdir):
    _write(os.path.join(workdir, "a.py"), "foo\n")
    _write(os.path.join(workdir, "b.py"), "bar\n")
    r = execute("grep", {"pattern": "foo", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "a.py"


def test_grep_count(workdir):
    _write(os.path.join(workdir, "a.py"), "foo\nfoo\nbar\n")
    r = execute("grep", {"pattern": "foo", "output_mode": "count"}, workdir)
    assert r.ok and r.output == "a.py:2"


def test_grep_ignore_case(workdir):
    _write(os.path.join(workdir, "a.py"), "Hello\n")
    r = execute("grep", {"pattern": "hello"}, workdir)
    assert r.ok and "(no matches)" in r.output
    r = execute("grep", {"pattern": "hello", "ignore_case": True}, workdir)
    assert r.ok and "a.py:1:Hello" in r.output


def test_grep_include(workdir):
    _write(os.path.join(workdir, "a.py"), "foo\n")
    _write(os.path.join(workdir, "b.txt"), "foo\n")
    r = execute("grep", {"pattern": "foo", "include": "*.py"}, workdir)
    assert r.ok and "a.py" in r.output and "b.txt" not in r.output


def test_grep_sorted_multiple(workdir):
    _write(os.path.join(workdir, "b.py"), "foo\n")
    _write(os.path.join(workdir, "a.py"), "foo\nfoo\n")
    r = execute("grep", {"pattern": "foo", "output_mode": "count"}, workdir)
    assert r.ok and r.output.splitlines() == ["a.py:2", "b.py:1"]


def test_grep_no_match(workdir):
    _write(os.path.join(workdir, "a.py"), "abc\n")
    r = execute("grep", {"pattern": "zzz"}, workdir)
    assert r.ok and "(no matches)" in r.output


def test_grep_binary_skipped(workdir):
    _write(os.path.join(workdir, "a.bin"), "\x00\x01\x02")
    r = execute("grep", {"pattern": "x", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and "(no matches)" in r.output


def test_grep_protected_skipped(workdir):
    _write(os.path.join(workdir, ".env"), "secret\n")
    _write(os.path.join(workdir, "a.py"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and "a.py" in r.output and ".env" not in r.output


def test_grep_single_file(workdir):
    _write(os.path.join(workdir, "sub", "a.py"), "foo\n")
    r = execute("grep", {"pattern": "foo", "path": "sub/a.py"}, workdir)
    assert r.ok and "sub/a.py:1:foo" in r.output


def test_grep_single_file_gitignored(workdir):
    _write(os.path.join(workdir, ".gitignore"), "a.py\n")
    _write(os.path.join(workdir, "a.py"), "secret\n")
    r = execute("grep", {"pattern": "secret", "path": "a.py"}, workdir)
    assert r.ok and "(no matches)" in r.output


def test_grep_invalid_regex(workdir):
    r = execute("grep", {"pattern": "("}, workdir)
    assert not r.ok and "invalid regex" in r.output


def test_grep_long_line_clipped(workdir):
    _write(os.path.join(workdir, "a.py"), "x" * 300 + "\n")
    r = execute("grep", {"pattern": "x"}, workdir)
    assert r.ok and "a.py:1:" in r.output and "..." in r.output


def test_grep_result_cap_truncated(workdir):
    _write(os.path.join(workdir, "a.py"), "hit\n" * 500)
    _write(os.path.join(workdir, "b.py"), "hit\n" * 100)
    r = execute("grep", {"pattern": "hit"}, workdir)
    assert r.ok and r.truncated
    assert "search truncated" in r.output


def test_parse_gitignore_lines():
    from code_agent.tools import _parse_gitignore_lines
    rules = _parse_gitignore_lines("# c\n\n*.log\n/build\n!keep.txt\ndir/\n", "/wd")
    assert len(rules) == 4
    assert rules[0]["pattern"] == "*.log" and not rules[0]["negation"]
    assert rules[1]["anchored"] is True and rules[1]["pattern"] == "build"
    assert rules[2]["negation"] is True and rules[2]["pattern"] == "keep.txt"
    assert rules[3]["dir_only"] is True and rules[3]["pattern"] == "dir"


def test_grep_gitignore_skips(workdir):
    _write(os.path.join(workdir, ".gitignore"), "ignored.txt\n")
    _write(os.path.join(workdir, "ignored.txt"), "secret\n")
    _write(os.path.join(workdir, "kept.txt"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "kept.txt"


def test_grep_gitignore_nested(workdir):
    _write(os.path.join(workdir, "sub", ".gitignore"), "skipme.txt\n")
    _write(os.path.join(workdir, "sub", "skipme.txt"), "secret\n")
    _write(os.path.join(workdir, "sub", "keep.txt"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "sub/keep.txt"


def test_grep_gitignore_comment_and_dir(workdir):
    _write(os.path.join(workdir, ".gitignore"), "# comment\n\nbuild/\n")
    _write(os.path.join(workdir, "build", "out.txt"), "secret\n")
    _write(os.path.join(workdir, "src", "ok.py"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "src/ok.py"


def test_grep_gitignore_negation(workdir):
    _write(os.path.join(workdir, ".gitignore"), "*.log\n!keep.log\n")
    _write(os.path.join(workdir, "a.log"), "secret\n")
    _write(os.path.join(workdir, "keep.log"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "keep.log"


def test_grep_gitignore_anchored(workdir):
    _write(os.path.join(workdir, ".gitignore"), "/root.txt\n")
    _write(os.path.join(workdir, "root.txt"), "secret\n")
    _write(os.path.join(workdir, "sub", "root.txt"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and r.output == "sub/root.txt"


def test_read_code_agent_protected(workdir):
    _write(os.path.join(workdir, ".code_agent", "session.jsonl"), "secret\n")
    r = execute("read_file", {"path": ".code_agent/session.jsonl"}, workdir)
    assert not r.ok and "protected" in r.output


def test_write_code_agent_protected(workdir):
    r = execute("write_file", {"path": ".code_agent/x.txt", "content": "x"}, workdir)
    assert not r.ok and "protected" in r.output


def test_grep_skips_code_agent(workdir):
    _write(os.path.join(workdir, ".code_agent", "session.jsonl"), "secret\n")
    _write(os.path.join(workdir, "a.py"), "secret\n")
    r = execute("grep", {"pattern": "secret", "output_mode": "files_with_matches"}, workdir)
    assert r.ok and "a.py" in r.output and ".code_agent" not in r.output


def test_execute_use_skill_unknown_without_registry(workdir):
    r = execute("use_skill", {"name": "x"}, workdir)
    assert not r.ok and "unknown tool" in r.output
