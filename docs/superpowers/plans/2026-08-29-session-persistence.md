# 会话持久化 + 多会话管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 会话对话持久化到 `<workdir>/.code_agent/sessions/<id>.jsonl`，支持新建/列出/恢复会话；交互模式跨重启续接。

**Architecture:** 新增 `session.py`（`SessionStore`，纯 stdlib json）；`context.py` 增加 `Conversation.to_jsonl/from_jsonl`；`agent.py` 的 `AgentSession` 接受可选 `store`/`session_id`/`resume`，每次 `run_task` 结束原子保存，并提供 `new_session`/`load_session`；`cli.py` 增加 `--list-sessions`/`--resume` 与交互斜杠命令。`.code_agent` 加入受保护路径。

**Tech Stack:** Python 3.11+ 标准库（json/os/datetime）。测试框架 pytest。

**Spec:** `docs/superpowers/specs/2026-08-29-session-persistence-design.md`

## Global Constraints

- Python 3.11+；零新依赖（标准库 json/os/datetime）。
- 存储：`<workdir>/.code_agent/sessions/<session_id>.jsonl`；`session_id = strftime("code_agent-%Y%m%d-%H%M%S%f")`（微秒精度防同秒碰撞）。
- 文件首行 meta：`{"type":"meta","id","title","created_at","updated_at","message_count"}`（`datetime.now().isoformat(timespec="microseconds")`）。
- 保存为全量原子写（tmp + `os.replace`）；标题 = 首条 user 消息去换行前 40 字符。
- `save` 保留原 `created_at`；`load` 文件缺失抛 `KeyError`，坏行跳过不中断。
- 恢复时重注入当前 `SYSTEM_PROMPT`（移除旧 system）。
- 交互斜杠命令：`/new` `/list` `/resume <id>`；`/exit` 等价现有 exit/quit。
- 现有无 store 的行为不得改变（既有测试全绿）。
- `.code_agent` 为受保护路径组件（tools.py `_is_protected_path`）；`.gitignore` 忽略 `.code_agent/`。
- 测试全部离线；`uv run pytest tests/ -q` 全绿后提交。
- 无凭据入库；提交保留完整历史，不 rebase。

---

### Task 1: SessionStore（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Create: `code_agent/code_agent/session.py`
- Create: `code_agent/tests/test_session.py`

**Interfaces:**
- Produces:
  - `_make_session_id(now: datetime | None = None) -> str`
  - `_make_title(task: str) -> str`（40 字符截断，空白折叠）
  - `class SessionStore(root: str)`
    - `list_sessions() -> list[dict]`：`{id,title,created_at,updated_at,message_count}`，按 `updated_at` 倒序；目录缺失返回 `[]`；meta 行缺失/损坏的会话跳过。
    - `create(title: str) -> str`：写 meta-only jsonl，返回 session_id。
    - `save(session_id, messages: list[dict], title: str | None = None) -> None`：全量原子写；保留原 created_at；原文件缺失则新建（message_count=len(messages)）。
    - `load(session_id) -> tuple[dict, list[dict]]`：返回 (meta, messages)；文件缺失抛 `KeyError(session_id)`；坏行跳过。
  - Task 3 依赖 `SessionStore` 与 `_make_title`。

- [ ] **Step 1: 写失败测试**

创建 `code_agent/tests/test_session.py`：

```python
import os

import pytest

from code_agent.session import SessionStore, _make_title


def test_create_writes_meta_file(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("hello task")
    assert os.path.isfile(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    meta, msgs = store.load(sid)
    assert meta["type"] == "meta" and meta["title"] == "hello task"
    assert msgs == []


def test_save_load_roundtrip(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
    ]
    store.save(sid, messages)
    meta, loaded = store.load(sid)
    assert loaded == messages
    assert meta["message_count"] == 3


def test_list_sessions_sorted_by_updated(tmp_path):
    store = SessionStore(str(tmp_path))
    a = store.create("first")
    b = store.create("second")
    store.save(b, [{"role": "user", "content": "b2"}])
    sessions = store.list_sessions()
    assert [s["id"] for s in sessions] == [b, a]
    assert sessions[0]["message_count"] == 1
    assert sessions[1]["message_count"] == 0


def test_list_sessions_missing_dir(tmp_path):
    store = SessionStore(str(tmp_path / "nope"))
    assert store.list_sessions() == []


def test_load_missing_raises_keyerror(tmp_path):
    store = SessionStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.load("code_agent-20260829-000000")


def test_load_skips_corrupt_lines(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = "code_agent-test"
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"type":"meta","id":"' + sid + '"}\n')
        f.write("garbage-line\n")
        f.write('{"role":"user","content":"ok"}\n')
    meta, msgs = store.load(sid)
    assert meta["id"] == sid
    assert msgs == [{"role": "user", "content": "ok"}]


def test_save_keeps_created_at(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    meta0, _ = store.load(sid)
    store.save(sid, [{"role": "user", "content": "x"}])
    meta1, _ = store.load(sid)
    assert meta1["created_at"] == meta0["created_at"]
    assert meta1["message_count"] == 1


def test_make_title_truncates():
    assert _make_title("a" * 100) == "a" * 40
    assert _make_title("  hi   there  ") == "hi there"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_session.py -v`
Expected: FAIL（ModuleNotFoundError: code_agent.session）

- [ ] **Step 3: 实现 session.py**

创建 `code_agent/code_agent/session.py`：

```python
"""Session persistence: JSONL storage of conversation messages."""
from __future__ import annotations

import json
import os
from datetime import datetime

TITLE_MAX_CHARS = 40


def _make_session_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("code_agent-%Y%m%d-%H%M%S")


def _make_title(task: str) -> str:
    return " ".join(task.split())[:TITLE_MAX_CHARS]


def _meta_dict(session_id: str, title: str, created_at: str, updated_at: str, message_count: int) -> dict:
    return {
        "type": "meta",
        "id": session_id,
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "message_count": message_count,
    }


class SessionStore:
    def __init__(self, root: str) -> None:
        self.root = root

    @staticmethod
    def _path(root: str, session_id: str) -> str:
        return os.path.join(root, f"{session_id}.jsonl")

    def _read_meta(self, path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("type") == "meta":
                        return obj
                    break
        except OSError:
            return None
        return None

    def list_sessions(self) -> list[dict]:
        if not os.path.isdir(self.root):
            return []
        out: list[dict] = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".jsonl"):
                continue
            meta = self._read_meta(os.path.join(self.root, name))
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def create(self, title: str) -> str:
        os.makedirs(self.root, exist_ok=True)
        session_id = _make_session_id()
        now = datetime.now().isoformat(timespec="microseconds")
        meta = _meta_dict(session_id, title, now, now, 0)
        self._write(self._path(self.root, session_id), [meta])
        return session_id

    def save(self, session_id: str, messages: list[dict], title: str | None = None) -> None:
        path = self._path(self.root, session_id)
        existing = self._read_meta(path) if os.path.isfile(path) else None
        now = datetime.now().isoformat(timespec="microseconds")
        created_at = existing["created_at"] if existing else now
        if existing is None:
            os.makedirs(self.root, exist_ok=True)
        resolved_title = title if title is not None else (existing.get("title") if existing else "")
        meta = _meta_dict(session_id, resolved_title, created_at, now, len(messages))
        self._write(path, [meta] + list(messages))

    def load(self, session_id: str) -> tuple[dict, list[dict]]:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            raise KeyError(session_id)
        meta: dict | None = None
        messages: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "meta":
                    meta = obj
                    continue
                messages.append(obj)
        if meta is None:
            raise KeyError(session_id)
        return meta, messages

    def _write(self, path: str, objects: list) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for obj in objects:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_session.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/session.py tests/test_session.py
git commit -m "feat: 会话持久化 SessionStore（JSONL，原子写，ADR-012）"
```

---

### Task 2: Conversation 序列化（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/context.py`
- Modify: `code_agent/tests/test_context.py`

**Interfaces:**
- Consumes: 无（仅 context.py 内部）。
- Produces:
  - `Conversation.to_jsonl(self) -> str`：逐行 `json.dumps(msg, ensure_ascii=False)`，行尾 `\n`。
  - `Conversation.from_jsonl(text: str, system_prompt: str | None = None) -> Conversation`（classmethod）：跳过非 dict 与无 `role` 的行；若给 `system_prompt` 移除旧 system 并插入当前。
  - Task 3 依赖 `from_jsonl`。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_context.py` 末尾追加：

```python
def test_to_jsonl_from_jsonl_roundtrip():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    conv.add_assistant("", [_TC("c1", "read_file", {"path": "a"})])
    conv.add_tool("c1", "read_file", "ok")
    text = conv.to_jsonl()
    restored = Conversation.from_jsonl(text)
    assert restored.messages == conv.messages
    assert restored.is_valid()


def test_from_jsonl_reinjects_system():
    conv = Conversation()
    conv.add_system("old-system")
    conv.add_user("u")
    text = conv.to_jsonl()
    restored = Conversation.from_jsonl(text, system_prompt="new-system")
    assert restored.messages[0] == {"role": "system", "content": "new-system"}
    assert sum(1 for m in restored.messages if m["role"] == "system") == 1


def test_from_jsonl_skips_bad_lines():
    conv = Conversation.from_jsonl('not-json\n{}\n{"role":"user","content":"u"}\n')
    assert conv.messages == [{"role": "user", "content": "u"}]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_context.py -v -k "jsonl"`
Expected: FAIL（AttributeError: 'Conversation' object has no attribute 'to_jsonl'）

- [ ] **Step 3: 实现序列化**

在 `code_agent/code_agent/context.py` 的 `Conversation` 类中，`is_valid` 之后、`build_messages` 之前追加：

```python
    def to_jsonl(self) -> str:
        lines = [json.dumps(m, ensure_ascii=False) for m in self._messages]
        return "\n".join(lines) + "\n" if lines else ""

    @classmethod
    def from_jsonl(cls, text: str, system_prompt: str | None = None) -> "Conversation":
        conv = cls()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or "role" not in obj:
                continue
            conv._messages.append(obj)
        if system_prompt is not None:
            conv._messages = [m for m in conv._messages if m.get("role") != "system"]
            conv._messages.insert(0, {"role": "system", "content": system_prompt})
        return conv
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_context.py -v`
Expected: 全绿（含 3 个新用例）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/context.py tests/test_context.py
git commit -m "feat: Conversation to_jsonl/from_jsonl 序列化（ADR-012）"
```

---

### Task 3: AgentSession 集成（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Modify: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `SessionStore`/`_make_title`（Task 1）；`Conversation.from_jsonl`（Task 2）；`SYSTEM_PROMPT`（agent.py 内已有）。
- Produces:
  - `AgentSession.__init__(..., store: SessionStore | None = None, session_id: str | None = None, resume: bool = False)`
    - `resume=True` 且无 `session_id` → `ValueError`。
    - `resume=True` → `load_session(session_id)`。
  - `AgentSession.new_session()`：重置 Conversation（当前 SYSTEM_PROMPT）+ `session_id=None`。
  - `AgentSession.load_session(session_id)`：从 store 加载重建 Conversation（重注入 SYSTEM_PROMPT）。
  - `run_task` 结束后（finally）：若 `store` 存在，首次自动 `store.create(_title())` 生成 `session_id`，然后 `store.save(session_id, conversation.messages, title=_title())`。
  - Task 4 依赖 `new_session`/`load_session`/`session_id`/`store`。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_agent.py` 顶部加 `import pytest`。在文件末尾追加：

```python
def test_agent_with_store_saves_after_task(workdir, tmp_path):
    from code_agent.session import SessionStore
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    store = SessionStore(str(tmp_path / "sessions"))
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store)
    result = session.run_task("read the file")
    assert result.finished
    assert session.session_id is not None
    meta, msgs = store.load(session.session_id)
    assert meta["title"] == "read the file"
    assert any(m.get("role") == "tool" for m in msgs)


def test_agent_resume_restores_conversation(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("resume-me")
    store.save(sid, [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old reply"},
    ])
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store, session_id=sid, resume=True)
    assert session.session_id == sid
    result = session.run_task("continue")
    assert result.finished
    contents = [m.get("content") for m in session.conversation.messages]
    assert "old task" in contents
    assert "continue" in contents


def test_agent_resume_without_session_id_raises(workdir):
    from code_agent.session import SessionStore
    store = SessionStore("/tmp/nonexistent-sessions")
    with pytest.raises(ValueError):
        AgentSession(workdir=workdir, llm=FakeLLM([]), store=store, resume=True)


def test_agent_load_session_switches_conversation(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("other")
    store.save(sid, [{"role": "user", "content": "other history"}])
    llm = FakeLLM([])
    session = AgentSession(workdir=workdir, llm=llm, store=store)
    session.load_session(sid)
    assert session.session_id == sid
    contents = [m.get("content") for m in session.conversation.messages]
    assert "other history" in contents
    assert session.conversation.is_valid()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v -k "store or resume or load_session"`
Expected: FAIL（TypeError: unexpected keyword argument 'store'）

- [ ] **Step 3: 实现 AgentSession 集成**

在 `code_agent/code_agent/agent.py`：

顶部 import 追加 `import json` 与：

```python
from code_agent.session import SessionStore, _make_title
```

`__init__` 签名与逻辑更新为：

```python
    def __init__(
        self,
        *,
        workdir: str,
        llm: Any,
        max_iterations: int = MAX_ITERATIONS_DEFAULT,
        max_context_tokens: int = 90000,
        debug: bool = False,
        store: SessionStore | None = None,
        session_id: str | None = None,
        resume: bool = False,
    ) -> None:
        self.workdir = workdir
        self.llm = llm
        self.max_iterations = max_iterations
        self.max_context_tokens = max_context_tokens
        self.debug = debug
        self.store = store
        self.session_id = session_id
        if resume:
            if session_id is None:
                raise ValueError("resume=True requires session_id")
            self.load_session(session_id)
        else:
            self.conversation = Conversation()
            self.conversation.add_system(SYSTEM_PROMPT)
```

在 `run_task` 之后、`_run_tool` 之前追加方法（并将 `run_task` 主体包进 `try:`，末尾加 `finally:` 保存）：

```python
    def _title(self) -> str:
        for m in self.conversation.messages:
            if m.get("role") == "user":
                return _make_title(str(m.get("content", "")))
        return ""

    def new_session(self) -> None:
        self.conversation = Conversation()
        self.conversation.add_system(SYSTEM_PROMPT)
        self.session_id = None

    def load_session(self, session_id: str) -> None:
        if self.store is None:
            raise ValueError("no session store configured")
        _, messages = self.store.load(session_id)
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages)
        self.conversation = Conversation.from_jsonl(text, system_prompt=SYSTEM_PROMPT)
        self.session_id = session_id
```

`run_task` 重构为（整体循环体移入 `try`，`finally` 保存）：

```python
    def run_task(self, task: str, on_delta: Callable[[str], None] | None = None) -> RunResult:
        self.conversation.add_user(task)
        consecutive_failures = 0
        llm_error_count = 0
        try:
            for iteration in range(1, self.max_iterations + 1):
                if self.debug:
                    print(f"[agent] iteration {iteration}")
                messages = self.conversation.build_messages(self.max_context_tokens)
                try:
                    response = self.llm.chat(messages, tools=TOOL_SCHEMAS, on_delta=on_delta)
                except LLMError as e:
                    llm_error_count += 1
                    if llm_error_count >= MAX_CONSECUTIVE_FAILURES:
                        return RunResult(
                            final_text="",
                            iterations=iteration,
                            finished=False,
                            reason=f"llm error: {e}",
                        )
                    self.conversation.add_user(
                        f"[system] An LLM error occurred: {e}. "
                        "Please reply in plain text without tool calls, or continue if possible."
                    )
                    continue
                llm_error_count = 0
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
        finally:
            if self.store is not None:
                if self.session_id is None:
                    self.session_id = self.store.create(self._title())
                self.store.save(self.session_id, self.conversation.messages, title=self._title())
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v`
Expected: 全绿（原 9 用例 + 新增 4 用例）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: AgentSession 集成会话持久化（store/resume/new_session/load_session，ADR-012）"
```

---

### Task 4: CLI 参数与斜杠命令（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/cli.py`
- Modify: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `AgentSession`（含 `store`/`session_id`/`new_session`/`load_session`，Task 3）；`SessionStore`（Task 1）。
- Produces:
  - 新参数：`--list-sessions`（store_true）、`--resume <id>`。
  - `_make_store(workdir: str) -> SessionStore`（root = `workdir/.code_agent/sessions`）。
  - `_handle_command(command: str, session, store) -> bool`（返回 False 表示应退出）。
  - `main`：`--list-sessions` 先处理退出；`--resume` 不存在 → stderr 报错退出码 1；交互模式打印当前 session_id。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_cli.py` 的 `_FakeSession` 更新为（追加 `store`/`session_id`/`new_session`/`load_session`）：

```python
class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.store = kwargs.get("store")
        self.session_id = None

    def run_task(self, task, on_delta=None):
        on_delta("hello")
        return RunResult(final_text="hello", iterations=1, finished=True, reason="complete")

    def new_session(self):
        self.session_id = None

    def load_session(self, session_id):
        self.session_id = session_id
```

文件末尾追加：

```python
def test_main_list_sessions(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("hello task")
    rc = main(["--list-sessions", "--workdir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert sid in out and "hello task" in out


def test_main_resume_not_found(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    rc = main(["--resume", "code_agent-nonexistent", "--prompt", "x", "--workdir", str(tmp_path)])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_main_interactive_slash_commands(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("existing")
    store.save(sid, [{"role": "user", "content": "hi"}])
    inputs = iter(["/list", f"/resume {sid}", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert sid in out and rc == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v`
Expected: 新用例 FAIL（`--list-sessions` 无法识别 / `code_agent-nonexistent` 无报错）

- [ ] **Step 3: 实现 CLI**

`code_agent/code_agent/cli.py`：

顶部 import 追加 `from code_agent.session import SessionStore`。

`_build_parser` 追加两个参数（`--interactive` 之后）：

```python
    parser.add_argument("--list-sessions", action="store_true", help="List saved sessions and exit")
    parser.add_argument("--resume", help="Resume a session by id")
```

新增 `_make_store` 与 `_handle_command`（放在 `_run` 之后）：

```python
def _make_store(workdir: str) -> SessionStore:
    return SessionStore(os.path.join(workdir, ".code_agent", "sessions"))


def _handle_command(command: str, session, store: SessionStore) -> bool:
    parts = command.split(maxsplit=1)
    cmd = parts[0]
    if cmd == "/new":
        session.new_session()
        print("New session started.")
        return True
    if cmd == "/list":
        for s in store.list_sessions():
            print(f"{s['id']}  {s['title'] or ''}  ({s['message_count']} msgs)")
        return True
    if cmd == "/resume":
        sid = parts[1] if len(parts) > 1 else ""
        if not sid:
            print("usage: /resume <session-id>")
            return True
        try:
            session.load_session(sid)
            print(f"Resumed session {sid}.")
        except KeyError:
            print(f"session not found: {sid}", file=sys.stderr)
        return True
    if cmd == "/exit":
        return False
    print(f"unknown command: {cmd}")
    return True
```

`main` 重构：

```python
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.prompt and not args.interactive and not args.list_sessions:
        parser.print_help()
        return 1
    _load_dotenv()
    workdir = os.path.abspath(args.workdir)
    store = _make_store(workdir)
    if args.list_sessions:
        for s in store.list_sessions():
            print(f"{s['id']}  {s['title'] or ''}  ({s['message_count']} msgs, {s['updated_at']})")
        return 0
    llm = _make_client(args)
    try:
        session = AgentSession(
            workdir=workdir,
            llm=llm,
            max_iterations=args.max_iterations,
            max_context_tokens=args.max_context_tokens,
            debug=args.debug,
            store=store,
            session_id=args.resume,
            resume=args.resume is not None,
        )
    except KeyError:
        print(f"session not found: {args.resume}", file=sys.stderr)
        return 1
    if args.prompt:
        _run(session, args.prompt)
        return 0
    print("Interactive mode. Type 'exit', 'quit' or '/exit' to leave. Commands: /new /list /resume <id>")
    while True:
        try:
            task = input("> ")
        except EOFError:
            break
        task = task.strip()
        if not task:
            continue
        if task.lower() in {"exit", "quit"} or task == "/exit":
            break
        if task.startswith("/"):
            if not _handle_command(task, session, store):
                break
            continue
        _run(session, task)
        if session.session_id:
            print(f"[session {session.session_id}]")
    return 0
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v`
Expected: 全绿（原 7 用例 + 新增 3 用例）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: CLI --list-sessions/--resume 与交互斜杠命令（ADR-012）"
```

---

### Task 5: .code_agent 受保护路径（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/tools.py`
- Modify: `code_agent/.gitignore`
- Modify: `code_agent/tests/test_tools.py`

**Interfaces:**
- Consumes: 无。
- Produces: `.code_agent` 路径组件在 `_is_protected_path` 中被拒绝（read/write/edit/glob/grep/list_dir 全生效）。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_tools.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_tools.py -v -k "code_agent"`
Expected: FAIL（read 返回 ok=True；write 返回 ok=True；grep 命中 session.jsonl）

- [ ] **Step 3: 实现受保护路径**

`code_agent/code_agent/tools.py` `_is_protected_path` 中，`.env` 判断之后追加：

```python
        if part == ".code_agent":
            return True
```

`code_agent/.gitignore` 追加一行：

```
.code_agent/
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_tools.py -v -k "code_agent" && uv run pytest tests/ -q`
Expected: `-k "code_agent"` 全绿；全量通过

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/tools.py .gitignore tests/test_tools.py
git commit -m "feat: .code_agent 加入受保护路径并 gitignore（ADR-012）"
```

---

### Task 6: 文档同步 + ADR-012（实现 → 验证 → 提交）

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/docs/context-management.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（工作区根，仓库外，**不入库**，ADR-007）

**Interfaces:** 无。

- [ ] **Step 1: architecture.md**

模块总览表加一行：

```markdown
| `session.py` | 会话持久化：SessionStore（JSONL 存储/列表/恢复） | 无（纯逻辑，标准库） |
```

§3 新增 `### session.py` 节（放在 tools.py 之后）：

```markdown
### session.py
- `SessionStore(root)` — root 为 `<workdir>/.code_agent/sessions`。
- `list_sessions() -> list[dict]`（按 updated_at 倒序，含 message_count）/ `create(title) -> session_id` / `save(session_id, messages, title=None)`（全量原子写）/ `load(session_id) -> (meta, messages)`（缺失抛 KeyError，坏行跳过）。
```

数据流小节更新（cli 段追加一句）：`cli 通过 _make_store(workdir) 构造 SessionStore，AgentSession 每轮 run_task 结束自动保存会话。`

- [ ] **Step 2: design.md**

§6 "v0.1.0 已实现范围"追加：

```markdown
- [x] 会话持久化与多会话管理（JSONL 存储，--list-sessions/--resume，交互斜杠命令 /new /list /resume）
```

§8 开发路线追加：

```markdown
9. [x] 迭代增强：会话持久化 + 多会话管理（ADR-012，设计见 docs/superpowers/specs/2026-08-29-session-persistence-design.md）
```

- [ ] **Step 3: development.md**

§2 运行方式追加参数说明（`--help` 列表之后）：

```markdown
- `--list-sessions`：列出 `<workdir>/.code_agent/sessions/` 下的会话（id/标题/消息数/更新时间）。
- `--resume <id>`：恢复指定会话（可与 `--prompt`/`--interactive` 组合）。
- 交互模式斜杠命令：`/new`（新建）、`/list`（列出）、`/resume <id>`（恢复）、`/exit`（退出）。
```

§3 测试目录说明更新用例数为 `104`（"当前 82 个用例" → "当前 104 个用例"），并加 `test_session.py` 说明行：

```markdown
  - `test_session.py`：SessionStore 创建/保存/加载/列表/坏文件容错。
```

- [ ] **Step 4: context-management.md**

新增章节（文件末尾，未来扩展之前）：

```markdown
## 8. 会话持久化

- 对话可通过 `Conversation.to_jsonl()` / `from_jsonl()` 序列化到 JSONL（逐行一条消息）。
- 存储由 `session.SessionStore` 管理：`<workdir>/.code_agent/sessions/<id>.jsonl`，首行 meta。
- `AgentSession` 每次 `run_task` 结束自动保存；`--resume <id>` / 交互 `/resume` 恢复会话（重新注入当前 system prompt）。
- `.code_agent` 为受保护路径，工具层不可读写。
```

- [ ] **Step 5: ADR-012**

`/home/kiki/workspace/code_agent_project/.agent/03-decisions.md` 的 `## 后续决策记录处` 之前追加：

```markdown
## ADR-012：会话持久化与多会话管理
- **日期**：2026-08-29
- **状态**：已实施
- **背景**：对话仅存内存，交互退出即丢，无新建/列出/恢复；业界（Claude Code JSONL sessions、OpenCode session 树）标配。
- **决策**：会话存 `<workdir>/.code_agent/sessions/<id>.jsonl`（首行 meta + 消息行）；SessionStore 原子写；CLI 加 `--list-sessions`/`--resume` 与交互斜杠命令；恢复重注入当前 SYSTEM_PROMPT；`.code_agent` 受保护。
- **理由**：纯标准库、与项目绑定、实现可控；为后续 checkpoint/skill 打地基。
- **影响**：新增 session.py；AgentSession 可选 store 参数（无 store 行为不变）；对话含任务内容故 `.code_agent/` gitignore。
```

- [ ] **Step 6: 验证与提交**

Run: `cd code_agent && uv run pytest tests/ -q`
Expected: 104 passed

Run: `cd code_agent && uv run python -m code_agent --help`
Expected: 含 `--list-sessions` / `--resume`

Run: `cd code_agent && git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

```bash
cd code_agent
git add docs/architecture.md docs/design.md docs/development.md docs/context-management.md
git commit -m "docs: 同步会话持久化文档并记录 ADR-012"
```

注意：ADR-012 更新的是工作区根 `.agent/03-decisions.md`（仓库外），本步 git add **不含** `.agent/`。

---

### Task 7: 真实 API 冒烟验证会话恢复（冒烟 → 回归 → 提交）

**Files:** 无（如冒烟发现问题，修复对应文件）。

**Interfaces:** 无。

- [ ] **Step 1: 准备冒烟目录与任务**

```bash
mkdir -p /tmp/code_agent_smoke2
cat > /tmp/code_agent_smoke2/demo.py <<'EOF'
def greet(name):
    return f"hello {name}"
EOF
```

- [ ] **Step 2: 运行冒烟（建会话）**

```bash
set -a; source /home/kiki/workspace/code_agent_project/code_agent/.env; set +a
cd /tmp/code_agent_smoke2
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent \
  --workdir /tmp/code_agent_smoke2 --prompt "给 demo.py 的 greet 补一行返回 length 的测试思路说明（直接文字回答即可）"
```

Expected: 正常输出最终答复；随后：

```bash
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent --workdir /tmp/code_agent_smoke2 --list-sessions
```

Expected: 列出 1 个会话（含刚才的标题）。

- [ ] **Step 3: 运行冒烟（resume 续接）**

```bash
SID=$(uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent --workdir /tmp/code_agent_smoke2 --list-sessions 2>/dev/null | head -1 | awk '{print $1}')
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent \
  --workdir /tmp/code_agent_smoke2 --resume "$SID" --prompt "总结我们刚才聊了什么"
```

Expected: agent 引用上一轮的会话内容（证明恢复成功）。

- [ ] **Step 4: 清理 + 全量回归 + 凭据复核 + 提交**

```bash
rm -rf /tmp/code_agent_smoke2
cd /home/kiki/workspace/code_agent_project/code_agent
uv run pytest tests/ -q
git grep -iE "sk-[a-zA-Z0-9]{10,}"
git status
```

Expected: 104 passed；凭据无命中。若冒烟无需代码修改，`git status` 干净则跳过提交，在报告中说明。

> 若无真实 API key 或网络不可用，改为在演示脚本中演示，并在报告标注"冒烟待真实验证"。
