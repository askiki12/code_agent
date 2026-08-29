# code_agent 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python 从零实现一个可运行的编程智能体：通过 OpenAI 兼容接口调用模型，自主调用 5 个本地工具（read_file / write_file / edit_file / list_dir / run_command）完成任务，支持一次性与交互式 CLI。

**Architecture:** 分层模块（方案 B）：`cli.py`（入口）→ `agent.py`（会话循环/终止/错误恢复）→ `context.py`（消息/token 预算/裁剪）→ `tools.py`（工具 schema+本地执行器）→ `llm.py`（OpenAI 兼容流式封装+tool_calls 解析）。每层只依赖相邻层接口，可独立测试。

**Tech Stack:** Python 3.11+，`requests`（唯一第三方依赖），`pytest`（开发）。运行目录约定：仓库根 = `code_agent/`，包名与目录同名（扁平布局）。

## Global Constraints

- **禁止**任何 agent 框架/SDK（LangChain、OpenAI Agents SDK、AutoGen 等）；**禁止**依赖服务端托管工具（Code Interpreter、Files API）；重要逻辑全部自实现。
- 仅允许 OpenAI 兼容 `/chat/completions` 接口 + 模型原生 tool calling。
- 凭据唯一来源为环境变量（`CODE_AGENT_BASE_URL` / `CODE_AGENT_API_KEY` / `CODE_AGENT_MODEL`）；`.env`、`.git`、key 文件为受保护路径，工具层禁读禁写。
- 工具输出默认上限 8000 字符；命令默认超时 120s；迭代上限默认 20；连续工具失败达 3 次即终止。
- 每个任务结束必须有可运行测试通过，并提交（保留完整历史，不 rebase/改写）。

---

### Task 1: 项目脚手架

**Files:**
- Create: `code_agent/pyproject.toml`
- Create: `code_agent/.gitignore`
- Create: `code_agent/code_agent/__init__.py`
- Create: `code_agent/code_agent/__main__.py`
- Create: `code_agent/code_agent/cli.py`（占位，Task 7 重写）
- Create: `code_agent/tests/test_smoke.py`
- Create: `code_agent/docs/development-plan.md`（本文件）

**Interfaces:**
- Produces: 包 `code_agent` 可导入（`__version__`）；`python -m code_agent` 可运行；`pytest` 可在仓库根收集用例。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_smoke.py`:
```python
import code_agent


def test_package_importable():
    assert code_agent.__version__ == "0.1.0"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_agent'`）

- [ ] **Step 3: 创建脚手架文件**

`code_agent/pyproject.toml`:
```toml
[project]
name = "code-agent"
version = "0.1.0"
description = "A self-built coding agent"
requires-python = ">=3.11"
dependencies = ["requests>=2.31"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["code_agent*"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

`code_agent/.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
.venv/
.env
```

`code_agent/code_agent/__init__.py`:
```python
"""A self-built coding agent."""
__version__ = "0.1.0"
```

`code_agent/code_agent/__main__.py`:
```python
from code_agent.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
```

`code_agent/code_agent/cli.py`:
```python
"""Command-line entry point (placeholder, replaced in Task 7)."""


def main(argv=None) -> int:
    print("code_agent: implementation in progress")
    return 0
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/ -v`
Expected: PASS（1 个用例）
Run: `python -m code_agent`
Expected: `code_agent: implementation in progress`

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml .gitignore code_agent/ tests/ docs/development-plan.md
git commit -m "chore: 脚手架 + 实现计划（Task 1/8）"
```

---

### Task 2: context.py —— 消息管理、token 估算、裁剪

**Files:**
- Create: `code_agent/code_agent/context.py`
- Create: `code_agent/tests/test_context.py`

**Interfaces:**
- Consumes: 无（纯标准库）。
- Produces:
  - `estimate_tokens(text: str) -> int`（启发式：CJK 字符≈1 token，其它字符≈每 4 字符 1 token）
  - `class Conversation`：`add_system(text)` / `add_user(text)` / `add_assistant(content, tool_calls=None)` / `add_tool(tool_call_id, name, output)` / `messages`（属性，返回拷贝）/ `is_valid() -> bool`（无悬空 tool 消息）/ `build_messages(max_tokens: int) -> list[dict]`（保留 system+最近消息，成组裁剪，不产生孤儿 tool）
  - `add_assistant` 的 `tool_calls` 参数接受任意含 `.id` / `.name` / `.arguments` 的对象列表（鸭子类型，Task 5 的 `ToolCall` 即满足）。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_context.py`:
```python
from code_agent.context import Conversation, estimate_tokens


class _TC:
    def __init__(self, id_, name, args):
        self.id = id_
        self.name = name
        self.arguments = args


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 1


def test_estimate_tokens_ascii():
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_cjk():
    assert estimate_tokens("你好世界") == 4


def test_add_messages_order():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    assert [m["role"] for m in conv.messages] == ["system", "user"]


def test_is_valid_true():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    conv.add_assistant("", [_TC("c1", "read_file", {})])
    conv.add_tool("c1", "read_file", "ok")
    assert conv.is_valid()


def test_is_valid_false_orphan_tool():
    conv = Conversation()
    conv.add_tool("orphan", "read_file", "x")
    assert not conv.is_valid()


def test_build_messages_keeps_system_and_order():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u1")
    conv.add_assistant("a1")
    conv.add_user("u2")
    msgs = conv.build_messages(100000)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "sys"


def test_build_messages_trims_old_when_over_budget():
    conv = Conversation()
    conv.add_system("sys")
    for i in range(10):
        conv.add_user(f"user message {i} " + "x" * 50)
        conv.add_assistant(f"assistant reply {i} " + "y" * 50)
    msgs = conv.build_messages(200)
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["content"].startswith("assistant reply 9")
    assert not any("assistant reply 0" in m["content"] for m in msgs)


def test_build_messages_grouping_no_orphan_tool():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    for i in range(5):
        conv.add_assistant("", [_TC(f"c{i}", "read_file", {})])
        conv.add_tool(f"c{i}", "read_file", "result " + "z" * 100)
    msgs = conv.build_messages(300)
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            assert msgs[i - 1]["role"] == "assistant"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_context.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_agent.context'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/context.py`:
```python
"""Message history, token estimation, and trimming."""
from __future__ import annotations

import json
import re
from typing import Any

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate: CJK chars ~1 token, other chars ~1 token / 4 chars."""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, cjk + other // 4)


class Conversation:
    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []

    def add_system(self, text: str) -> None:
        self._messages.append({"role": "system", "content": text})

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def add_assistant(self, content: str, tool_calls: list | None = None) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]
        self._messages.append(msg)

    def add_tool(self, tool_call_id: str, name: str, output: str) -> None:
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": output,
            }
        )

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def is_valid(self) -> bool:
        """True if no orphan tool message: every tool msg has a preceding assistant tool_call."""
        pending: set[str] = set()
        for msg in self._messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                pending.update(tc["id"] for tc in msg["tool_calls"])
            elif msg["role"] == "tool":
                if msg.get("tool_call_id") not in pending:
                    return False
                pending.discard(msg["tool_call_id"])
        return True

    def build_messages(self, max_tokens: int) -> list[dict[str, Any]]:
        system = [m for m in self._messages if m["role"] == "system"]
        rest = [m for m in self._messages if m["role"] != "system"]
        out = list(system)
        budget = max_tokens - sum(
            estimate_tokens(str(m.get("content", ""))) for m in system
        )
        if budget <= 0:
            return out
        groups = self._group(rest)
        selected: list[dict[str, Any]] = []
        for group in reversed(groups):
            cost = sum(estimate_tokens(str(m.get("content", ""))) for m in group)
            if budget - cost < 0:
                if not selected:
                    selected = self._truncate_group(group, budget)
                break
            budget -= cost
            selected = group + selected
        return out + selected

    @staticmethod
    def _group(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Bundle each assistant (with tool_calls) with its following tool messages."""
        groups: list[list[dict[str, Any]]] = []
        i, n = 0, len(messages)
        while i < n:
            msg = messages[i]
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                group = [msg]
                i += 1
                while i < n and messages[i]["role"] == "tool":
                    group.append(messages[i])
                    i += 1
                groups.append(group)
            else:
                groups.append([msg])
                i += 1
        return groups

    @staticmethod
    def _truncate_group(
        group: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        out = []
        for m in group:
            m = dict(m)
            if m["role"] == "tool":
                content = str(m.get("content", ""))
                if len(content) > budget:
                    m["content"] = content[:budget] + f"\n...[truncated {len(content) - budget} chars]"
            out.append(m)
        return out
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_context.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/context.py tests/test_context.py
git commit -m "feat: context.py 消息管理/token 估算/裁剪（Task 2/8）"
```

---

### Task 3: tools.py 第一部分 —— 基础设施 + 只读工具

**Files:**
- Create: `code_agent/code_agent/tools.py`
- Create: `code_agent/tests/test_tools.py`

**Interfaces:**
- Consumes: 无（纯标准库）。
- Produces:
  - `@dataclass ToolResult`：`ok: bool` / `output: str` / `truncated: bool = False` / `exit_code: int | None = None`，方法 `as_message() -> str`
  - `truncate(text: str, limit: int = 8000) -> tuple[str, bool]`
  - `TOOL_SCHEMAS: list[dict]`（5 个工具，OpenAI functions 格式）
  - `execute(name: str, args: dict, workdir: str) -> ToolResult`（含未知工具/异常兜底）
  - 内部：`_is_protected_path(path)`（`.env*`（除 `.env.example`）、`.git` 任意路径段命中即保护）、`_inside_workdir(path, workdir)`、`_resolve(path, workdir)`

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_tools.py`:
```python
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
```

`code_agent/tests/conftest.py`:
```python
import pytest


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_tools.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_agent.tools'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tools.py`:
```python
"""Tool definitions and local executors (all self-implemented)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 8000
DEFAULT_COMMAND_TIMEOUT = 120  # seconds
MAX_LISTING_ENTRIES = 200


@dataclass
class ToolResult:
    ok: bool
    output: str
    truncated: bool = False
    exit_code: int | None = None

    def as_message(self) -> str:
        return self.output


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    marker = f"\n...[TRUNCATED: {len(text) - limit} chars omitted]...\n"
    return head + marker + tail, True


def _is_protected_path(path: str) -> bool:
    parts = [p for p in path.replace(os.sep, "/").split("/") if p]
    for part in parts:
        if part == ".git":
            return True
        if part.startswith(".env") and part != ".env.example":
            return True
    return False


def _inside_workdir(path: str, workdir: str) -> bool:
    wd = os.path.realpath(workdir)
    p = os.path.realpath(path)
    try:
        return os.path.commonpath([wd, p]) == wd
    except ValueError:
        return False


def _resolve(path: str, workdir: str) -> str:
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(workdir, path))


def _read_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to read protected path: {path}")
    if not os.path.isfile(path):
        return ToolResult(ok=False, output=f"file not found: {path}")
    offset = args.get("offset")
    limit = args.get("limit")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return ToolResult(ok=False, output=f"read failed: {e}")
    start = offset - 1 if isinstance(offset, int) and offset >= 1 else 0
    end = start + limit if isinstance(limit, int) and limit >= 1 else len(lines)
    selected = lines[start:end]
    if start == 0 and end >= len(lines):
        body = "".join(selected)
    else:
        body = "".join(f"{start + i + 1:>6}  {line}" for i, line in enumerate(selected))
    body, truncated = truncate(body)
    return ToolResult(ok=True, output=body, truncated=truncated)


def _list_dir(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", "."), workdir)
    if not os.path.isdir(path):
        return ToolResult(ok=False, output=f"directory not found: {path}")
    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return ToolResult(ok=False, output=f"list failed: {e}")
    lines: list[str] = []
    shown = 0
    for e in entries:
        if e.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        if e.is_dir():
            lines.append(f"{e.name}/")
        else:
            try:
                size = os.path.getsize(e.path)
            except OSError:
                size = 0
            lines.append(f"{e.name}  ({size} bytes)")
        shown += 1
        if shown >= MAX_LISTING_ENTRIES:
            lines.append(f"...[{shown} entries shown, more omitted]")
            break
    if not lines:
        lines.append("(empty directory)")
    return ToolResult(ok=True, output="\n".join(lines))


def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _schema(
        "read_file",
        "Read a text file (with optional line range). Refuses protected paths.",
        {
            "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
            "offset": {"type": "integer", "description": "1-based start line"},
            "limit": {"type": "integer", "description": "Max lines to read"},
        },
        ["path"],
    ),
    _schema(
        "list_dir",
        "List directory entries with type and size. Skips .git and caches.",
        {"path": {"type": "string", "description": "Directory path (defaults to workdir)"}},
        [],
    ),
]

_HANDLERS = {
    "read_file": _read_file,
    "list_dir": _list_dir,
}


def execute(name: str, args: dict, workdir: str) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, output=f"unknown tool: {name}")
    try:
        return handler(args, workdir)
    except Exception as e:  # noqa: BLE001 - last-resort guard
        return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")
```

（Task 4 将向 `TOOL_SCHEMAS` 与 `_HANDLERS` 追加 write_file / edit_file / run_command。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_tools.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/tools.py tests/test_tools.py tests/conftest.py
git commit -m "feat: tools.py 基础设施与只读工具 read_file/list_dir（Task 3/8）"
```

---

### Task 4: tools.py 第二部分 —— 写工具与命令执行

**Files:**
- Modify: `code_agent/code_agent/tools.py`（追加 3 个工具）
- Modify: `code_agent/tests/test_tools.py`（追加用例）

**Interfaces:**
- Consumes: Task 3 的 `ToolResult` / `truncate` / `_is_protected_path` / `_inside_workdir` / `_resolve` / `TOOL_SCHEMAS` / `_HANDLERS` / `_schema`。
- Produces: `TOOL_SCHEMAS` 与 `_HANDLERS` 扩展至 5 个工具。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tools.py`:
```python
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
    r = execute("run_command", {"command": "python -c \"print('x' * 20000)\""}, workdir)
    assert r.ok and r.truncated
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_tools.py -v`
Expected: FAIL（write_file / edit_file / run_command 为 unknown tool）

- [ ] **Step 3: 最小实现**

在 `code_agent/code_agent/tools.py` 的 `_HANDLERS` 定义之前追加以下实现，并把三个 schema 追加到 `TOOL_SCHEMAS`：

```python
def _write_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to write protected path: {path}")
    if not _inside_workdir(path, workdir):
        return ToolResult(ok=False, output=f"refusing to write outside workdir: {path}")
    content = args.get("content", "")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError as e:
        return ToolResult(ok=False, output=f"write failed: {e}")
    return ToolResult(ok=True, output=f"wrote {len(content)} chars to {path}")


def _edit_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to edit protected path: {path}")
    if not _inside_workdir(path, workdir):
        return ToolResult(ok=False, output=f"refusing to edit outside workdir: {path}")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if old == "":
        return ToolResult(ok=False, output="old_string must not be empty")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return ToolResult(ok=False, output=f"read failed: {e}")
    count = content.count(old)
    if count == 0:
        return ToolResult(ok=False, output=f"old_string not found in {path}")
    if count > 1 and not args.get("replace_all"):
        return ToolResult(
            ok=False,
            output=f"old_string matches {count} times in {path}; provide more context or set replace_all=true",
        )
    content = content.replace(old, new)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError as e:
        return ToolResult(ok=False, output=f"write failed: {e}")
    return ToolResult(ok=True, output=f"applied {count} replacement(s) in {path}")


def _run_command(args: dict, workdir: str) -> ToolResult:
    command = args.get("command", "")
    if not command.strip():
        return ToolResult(ok=False, output="command must not be empty")
    timeout = float(args.get("timeout", DEFAULT_COMMAND_TIMEOUT))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, output=f"command timed out after {timeout}s")
    except OSError as e:
        return ToolResult(ok=False, output=f"failed to run command: {e}")
    out = proc.stdout
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    out, truncated = truncate(out)
    return ToolResult(
        ok=proc.returncode == 0,
        output=out,
        truncated=truncated,
        exit_code=proc.returncode,
    )
```

`TOOL_SCHEMAS` 追加：
```python
    _schema(
        "write_file",
        "Create or overwrite a file (must be inside the workdir).",
        {
            "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
            "content": {"type": "string", "description": "Full file content"},
        },
        ["path", "content"],
    ),
    _schema(
        "edit_file",
        "Replace an exact substring in a file (must match uniquely unless replace_all).",
        {
            "path": {"type": "string", "description": "File path"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        ["path", "old_string", "new_string"],
    ),
    _schema(
        "run_command",
        "Run a shell command in the workdir (timeout and output limits apply).",
        {
            "command": {"type": "string", "description": "Shell command"},
            "timeout": {"type": "number", "description": f"Timeout in seconds (default {DEFAULT_COMMAND_TIMEOUT})"},
        },
        ["command"],
    ),
```

`_HANDLERS` 更新为：
```python
_HANDLERS = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "run_command": _run_command,
}
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_tools.py -v`
Expected: PASS（全部用例，含 Task 3 的 5 工具 schema 名称断言）

- [ ] **Step 5: 提交**

```bash
git add code_agent/tools.py tests/test_tools.py
git commit -m "feat: tools.py 写工具与命令执行 write_file/edit_file/run_command（Task 4/8）"
```

---

### Task 5: llm.py —— 流式封装与 tool_calls 解析

**Files:**
- Create: `code_agent/code_agent/llm.py`
- Create: `code_agent/tests/test_llm_parse.py`

**Interfaces:**
- Consumes: `requests`（唯一第三方依赖）。
- Produces:
  - `class LLMError(Exception)`
  - `@dataclass ToolCall`：`id: str` / `name: str` / `arguments: dict`
  - `@dataclass LLMResponse`：`content: str` / `tool_calls: list[ToolCall]`
  - `parse_tool_arguments(raw: str) -> dict`（空→`{}`；非法 JSON 或非对象→`LLMError`）
  - `class _StreamAccumulator`：`feed(delta: dict)` / `result() -> LLMResponse`
  - `iter_sse_lines(response) -> Iterator[str]`（剥离 `data:` 前缀、遇 `[DONE]` 结束）
  - `class LLMClient`：`__init__(*, base_url, api_key, model, timeout=300.0, max_retries=3, debug=False)`；`chat(messages, tools=None, on_delta=None) -> LLMResponse`（流式 SSE，429/5xx/网络错误指数退避重试）

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_llm_parse.py`:
```python
import pytest

from code_agent.llm import (
    LLMError,
    _StreamAccumulator,
    iter_sse_lines,
    parse_tool_arguments,
)


def test_accumulate_content():
    acc = _StreamAccumulator()
    acc.feed({"content": "Hel"})
    acc.feed({"content": "lo"})
    resp = acc.result()
    assert resp.content == "Hello"
    assert resp.tool_calls == []


def test_accumulate_tool_call_fragments():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}]})
    acc.feed({"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a'}}]})
    acc.feed({"tool_calls": [{"index": 0, "function": {"arguments": '.txt"}'}}]})
    resp = acc.result()
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "a.txt"}


def test_accumulate_multiple_tool_calls():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [
        {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        {"index": 1, "id": "c2", "function": {"name": "list_dir", "arguments": "{}"}},
    ]})
    resp = acc.result()
    assert [tc.name for tc in resp.tool_calls] == ["read_file", "list_dir"]


def test_parse_tool_arguments_empty():
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("   ") == {}


def test_parse_tool_arguments_invalid():
    with pytest.raises(LLMError):
        parse_tool_arguments("{not json")


def test_parse_tool_arguments_nonobject():
    with pytest.raises(LLMError):
        parse_tool_arguments('"just a string"')


def test_result_raises_on_malformed_arguments():
    acc = _StreamAccumulator()
    acc.feed({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": "{"}}]})
    with pytest.raises(LLMError):
        acc.result()


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode):
        return iter(self._lines)


def test_iter_sse_lines_stops_at_done():
    resp = _FakeResponse(["data: {\"x\": 1}", "", ": keepalive", "data: [DONE]", "data: ignored"])
    assert list(iter_sse_lines(resp)) == ['{"x": 1}']
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_llm_parse.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_agent.llm'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/llm.py`:
```python
"""OpenAI-compatible chat client with streaming and tool-call parsing."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import requests


class LLMError(Exception):
    pass


class _Retryable(LLMError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]


@dataclass
class _StreamAccumulator:
    content: str = ""
    _calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def feed(self, delta: dict) -> None:
        if isinstance(delta.get("content"), str):
            self.content += delta["content"]
        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def result(self) -> LLMResponse:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            calls.append(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["arguments"]),
                )
            )
        return LLMResponse(content=self.content, tool_calls=calls)


def parse_tool_arguments(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"malformed tool arguments JSON: {e}: {raw[:200]}") from e
    if not isinstance(data, dict):
        raise LLMError(f"tool arguments must be a JSON object, got {type(data).__name__}")
    return data


def iter_sse_lines(response) -> Iterator[str]:
    """Yield decoded SSE data payloads; stop at [DONE]; skip comments/keepalives."""
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                return
            yield payload


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        max_retries: int = 3,
        debug: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self._request(payload, headers, on_delta)
            except _Retryable as e:
                last_error = e
            except (requests.RequestException, OSError) as e:
                last_error = e
            if attempt < self.max_retries - 1:
                if self.debug:
                    print(f"[llm] attempt {attempt + 1} failed: {last_error}; retrying in {2 ** attempt}s")
                time.sleep(2 ** attempt)
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    def _request(self, payload: dict, headers: dict, on_delta) -> LLMResponse:
        acc = _StreamAccumulator()
        with requests.post(
            self._url(), json=payload, headers=headers, stream=True, timeout=self.timeout
        ) as resp:
            if resp.status_code != 200:
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise _Retryable(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
                raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
            for payload_line in iter_sse_lines(resp):
                if not payload_line:
                    continue
                try:
                    chunk = json.loads(payload_line)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                acc.feed(delta)
                content = delta.get("content")
                if isinstance(content, str) and on_delta:
                    on_delta(content)
        return acc.result()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_llm_parse.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/llm.py tests/test_llm_parse.py
git commit -m "feat: llm.py OpenAI 兼容流式客户端与 tool_calls 解析（Task 5/8）"
```

---

### Task 6: agent.py —— 会话循环与终止条件

**Files:**
- Create: `code_agent/code_agent/agent.py`
- Create: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `context.Conversation`（Task 2）、`llm.LLMClient`（鸭子类型，可注入 Fake）、`tools.execute / TOOL_SCHEMAS / ToolResult`（Task 3-4）。
- Produces:
  - `@dataclass RunResult`：`final_text: str` / `iterations: int` / `finished: bool` / `reason: str`
  - `SYSTEM_PROMPT: str`、`MAX_ITERATIONS_DEFAULT = 20`、`MAX_CONSECUTIVE_FAILURES = 3`
  - `class AgentSession`：`__init__(*, workdir, llm, max_iterations=20, max_context_tokens=90000, debug=False)`；`run_task(task, on_delta=None) -> RunResult`；属性 `conversation`
  - 终止：无 tool_calls → `finished=True, reason="complete"`；达 `max_iterations` → `finished=False, reason="max_iterations"`；连续失败 ≥3 轮 → `finished=False, reason="too many consecutive tool failures"`

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_agent.py`:
```python
from pathlib import Path

from code_agent.agent import AgentSession
from code_agent.llm import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, on_delta=None):
        self.calls.append(messages)
        return self.responses.pop(0)


def _read_call(pid, path):
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=pid, name="read_file", arguments={"path": path})]
    )


def test_agent_completes_after_tool_round(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done: hello", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("read the file")
    assert result.finished and result.reason == "complete"
    assert result.final_text == "done: hello"
    assert result.iterations == 2
    assert session.conversation.is_valid()


def test_agent_max_iterations(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    call = _read_call("c1", "a.txt")
    llm = FakeLLM([call, call, call])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3)
    result = session.run_task("loop")
    assert not result.finished and result.reason == "max_iterations"
    assert result.iterations == 3


def test_agent_stops_on_consecutive_failures(workdir):
    bad = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="nonexistent_tool", arguments={})],
    )
    llm = FakeLLM([bad, bad, bad])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=10)
    result = session.run_task("boom")
    assert not result.finished
    assert result.reason == "too many consecutive tool failures"
    assert result.iterations == 3


def test_agent_recovers_after_failure(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "missing.txt"),
        _read_call("c2", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("recover")
    assert result.finished and result.final_text == "done"
    assert result.iterations == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_agent.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_agent.agent'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/agent.py`:
```python
"""Agent session loop, termination, and error recovery."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from code_agent.context import Conversation
from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute

SYSTEM_PROMPT = """You are a coding agent. You work inside a local workspace and complete software tasks autonomously.

Available tools:
- read_file: read text files
- write_file: create or overwrite files
- edit_file: replace an exact substring in a file (must match uniquely)
- list_dir: list directory contents
- run_command: run a shell command (has a timeout)

Rules:
1. Plan each step. Prefer small, verifiable changes.
2. Use run_command to verify your work (e.g. run tests).
3. When a tool fails, read the error, adjust, and retry. Do not repeat the exact same failing call.
4. Do NOT read or write protected paths such as .env, .env.* or .git.
5. When the task is complete, reply with a short final summary and stop making tool calls.
"""

MAX_ITERATIONS_DEFAULT = 20
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class RunResult:
    final_text: str
    iterations: int
    finished: bool
    reason: str


class AgentSession:
    def __init__(
        self,
        *,
        workdir: str,
        llm: Any,
        max_iterations: int = MAX_ITERATIONS_DEFAULT,
        max_context_tokens: int = 90000,
        debug: bool = False,
    ) -> None:
        self.workdir = workdir
        self.llm = llm
        self.max_iterations = max_iterations
        self.max_context_tokens = max_context_tokens
        self.debug = debug
        self.conversation = Conversation()
        self.conversation.add_system(SYSTEM_PROMPT)

    def run_task(self, task: str, on_delta: Callable[[str], None] | None = None) -> RunResult:
        self.conversation.add_user(task)
        consecutive_failures = 0
        for iteration in range(1, self.max_iterations + 1):
            if self.debug:
                print(f"[agent] iteration {iteration}")
            messages = self.conversation.build_messages(self.max_context_tokens)
            response = self.llm.chat(messages, tools=TOOL_SCHEMAS, on_delta=on_delta)
            self.conversation.add_assistant(response.content, response.tool_calls or None)
            if not response.tool_calls:
                return RunResult(
                    final_text=response.content,
                    iterations=iteration,
                    finished=True,
                    reason="complete",
                )
            round_failed = False
            for tc in response.tool_calls:
                result = self._run_tool(tc)
                if not result.ok:
                    round_failed = True
                if self.debug:
                    print(f"[tool] {tc.name}: ok={result.ok} truncated={result.truncated}")
                self.conversation.add_tool(tc.id, tc.name, result.as_message())
            consecutive_failures = consecutive_failures + 1 if round_failed else 0
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return RunResult(
                    final_text="",
                    iterations=iteration,
                    finished=False,
                    reason="too many consecutive tool failures",
                )
        return RunResult(
            final_text="",
            iterations=self.max_iterations,
            finished=False,
            reason="max_iterations",
        )

    def _run_tool(self, tc) -> ToolResult:
        try:
            return execute(tc.name, tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_agent.py -v`
Expected: PASS（全部用例）
Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（smoke 1 + context 9 + tools 24 + llm 8 + agent 4 = 46 用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: agent.py 会话循环/终止条件/错误恢复（Task 6/8）"
```

---

### Task 7: cli.py —— 命令行入口

**Files:**
- Modify: `code_agent/code_agent/cli.py`（替换占位）
- Create: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `AgentSession`（Task 6）、`LLMClient`（Task 5）。
- Produces:
  - `main(argv=None) -> int`：支持 `--prompt`（一次性）与 `--interactive`（交互式，`exit`/`quit`/EOF 退出）
  - 参数：`--workdir`（默认 `.`）、`--base-url`、`--api-key`、`--model`、`--max-iterations`（默认 20）、`--max-context-tokens`（默认 90000）、`--debug`
  - 环境变量：`CODE_AGENT_BASE_URL` / `CODE_AGENT_API_KEY`（缺失即报错退出）/ `CODE_AGENT_MODEL`；默认 `https://api.openai.com/v1` 与 `gpt-4o-mini`
  - 辅助：`_build_parser()`、`_make_client(args)`（无 key 时 `raise SystemExit`）

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_cli.py`:
```python
import pytest

from code_agent.cli import _build_parser, _make_client, main
from code_agent.agent import RunResult


def test_make_client_missing_key(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_API_KEY", raising=False)
    args = _build_parser().parse_args(["--prompt", "x"])
    with pytest.raises(SystemExit):
        _make_client(args)


def test_make_client_from_env(monkeypatch):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    args = _build_parser().parse_args(["--prompt", "x"])
    client = _make_client(args)
    assert client.api_key == "test-key"
    assert client.model == "gpt-4o-mini"


def test_parser_defaults():
    args = _build_parser().parse_args(["--prompt", "x"])
    assert args.max_iterations == 20
    assert args.max_context_tokens == 90000
    assert args.workdir == "."


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_task(self, task, on_delta=None):
        on_delta("hello")
        return RunResult(final_text="hello", iterations=1, finished=True, reason="complete")


def test_main_oneshot(monkeypatch, capsys):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--prompt", "do it", "--workdir", "/tmp"])
    assert rc == 0
    assert "hello" in capsys.readouterr().out
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL（`_build_parser` / `_make_client` 不存在或行为不符）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/cli.py`:
```python
"""Command-line entry point."""
from __future__ import annotations

import argparse
import os
import sys

from code_agent.agent import AgentSession
from code_agent.llm import LLMClient

_DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent", description="A self-built coding agent"
    )
    parser.add_argument("--prompt", help="Run a one-shot task and exit")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive session")
    parser.add_argument("--workdir", default=".", help="Working directory (default: current dir)")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="API key (default: env CODE_AGENT_API_KEY)")
    parser.add_argument("--model", help="Model name (default: env CODE_AGENT_MODEL)")
    parser.add_argument("--max-iterations", type=int, default=20, help="Max agent iterations")
    parser.add_argument("--max-context-tokens", type=int, default=90000, help="Context budget in tokens")
    parser.add_argument("--debug", action="store_true", help="Print debug logs")
    return parser


def _make_client(args: argparse.Namespace) -> LLMClient:
    base_url = args.base_url or os.environ.get("CODE_AGENT_BASE_URL") or _DEFAULTS["base_url"]
    api_key = args.api_key or os.environ.get("CODE_AGENT_API_KEY")
    if not api_key:
        raise SystemExit("error: CODE_AGENT_API_KEY is not set (or pass --api-key)")
    model = args.model or os.environ.get("CODE_AGENT_MODEL") or _DEFAULTS["model"]
    return LLMClient(base_url=base_url, api_key=api_key, model=model, debug=args.debug)


def _run(session: AgentSession, task: str) -> None:
    result = session.run_task(task, on_delta=lambda d: print(d, end="", flush=True))
    if result.final_text:
        print()
    if not result.finished:
        print(f"\n[agent] stopped: {result.reason}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.prompt and not args.interactive:
        parser.print_help()
        return 1
    workdir = os.path.abspath(args.workdir)
    llm = _make_client(args)
    session = AgentSession(
        workdir=workdir,
        llm=llm,
        max_iterations=args.max_iterations,
        max_context_tokens=args.max_context_tokens,
        debug=args.debug,
    )
    if args.prompt:
        _run(session, args.prompt)
        return 0
    print("Interactive mode. Type 'exit' or 'quit' to leave.")
    while True:
        try:
            task = input("> ")
        except EOFError:
            break
        task = task.strip()
        if not task:
            continue
        if task.lower() in {"exit", "quit"}:
            break
        _run(session, task)
    return 0
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS
Run: `python -m code_agent --help`
Expected: 正常输出帮助（exit 0）

- [ ] **Step 5: 提交**

```bash
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: cli.py 一次性与交互式入口（Task 7/8）"
```

---

### Task 8: 收尾验证与文档同步

**Files:**
- Modify: `code_agent/README.md`（补充用法）
- 其它：按需同步 `code_agent/docs/` 与 `.agent/`（本任务不新增文档内容，仅核对）

**Interfaces:**
- Consumes: 全部已实现模块。

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（smoke 1 + context 9 + tools 24 + llm 8 + agent 4 + cli 4 = 50 用例）

- [ ] **Step 2: CLI 冒烟**

Run: `python -m code_agent --help`
Expected: 帮助文本，exit 0
Run: `python -m code_agent --prompt x`
Expected: `error: CODE_AGENT_API_KEY is not set (or pass --api-key)`（无 key 时安全退出）

- [ ] **Step 3: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}" || echo "clean"`
Expected: 输出 `clean`（无命中）

- [ ] **Step 4: 更新 README.md**

`code_agent/README.md`:
```markdown
# code_agent

自研编程智能体（coding agent）：通过大语言模型自主读写文件、执行命令完成编程任务。

## 运行

```bash
export CODE_AGENT_BASE_URL="https://api.example.com/v1"
export CODE_AGENT_API_KEY="sk-..."   # 仅环境变量，切勿入库
export CODE_AGENT_MODEL="gpt-4o-mini"

# 一次性任务
python -m code_agent --prompt "把 tests/test_tools.py 里的测试全部跑通"

# 交互式
python -m code_agent --interactive
```

## 功能

- 5 个本地工具：read_file / write_file / edit_file / list_dir / run_command
- 流式输出、上下文 token 预算与自动裁剪、错误恢复与重试
- 受保护路径（.env / .git）禁读禁写；命令超时与输出截断

详见 `docs/`。
```

- [ ] **Step 5: 冒烟真实验证（可选，需真实 key）**

```bash
CODE_AGENT_API_KEY=... python -m code_agent --prompt "列出当前目录，然后读取 docs/design.md 的前 5 行，用一句话总结"
```
Expected: agent 调用 list_dir / read_file 后给出中文总结。
（若暂无可用 key，跳过后在演示阶段补做，并记录到文档 3。）

- [ ] **Step 6: 提交**

```bash
git add README.md
git commit -m "docs: README 运行说明与功能简介（Task 8/8）"
git log --oneline
```
Expected: 完整历史：Initial commit → docs 初始化 → Task 1~8。

---

## 验收对照（实现完成后逐条打勾）

- [ ] `python -m pytest tests/ -v` 全绿
- [ ] `python -m code_agent --help` 正常
- [ ] 无凭据入库（grep 复核 clean）
- [ ] 5 工具 schema 完整（`test_tool_schemas_have_expected_names` 覆盖）
- [ ] 上下文裁剪不产生孤儿 tool 消息（`test_context` 覆盖）
- [ ] 终止条件生效（正常 / max_iterations / 连续失败，`test_agent` 覆盖）
- [ ] mock 集成测试不依赖网络/真实 key（`test_agent` 覆盖）
- [ ] README.md 更新；提交历史完整
