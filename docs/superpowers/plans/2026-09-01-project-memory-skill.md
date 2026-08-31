# 项目记忆与经验沉淀 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 coding agent 增加 agent 原生的跨会话记忆与经验沉淀：`remember`/`recall`/`create_skill` 工具 + 任务开始时自动注入 top-K 记忆 + 任务成功结束自动总结写入记忆。

**Architecture:** 新增 `code_agent/memory.py`（`MemoryStore`，JSONL + 关键词相关度打分）；`skills.py` 加 `SkillRegistry.add`；`agent.py` 加三个 session-bound 工具（走 policy）与 `_inject_memory`/`_auto_memorize`/`_persist`，`run_task` 重构为 `_run_loop` + finally 持久化 + 成功自动总结；`cli.py` 开启 `memory=True`。记忆功能默认 `memory=False`（测试安全），子智能体关闭。

**Tech Stack:** Python 3.11+，pytest（离线测试）。

## Global Constraints

- 所有命令在 `code_agent/` 目录内经 `uv run` 执行。
- 记忆功能默认关闭（`AgentSession(memory=False)`）；`cli.main` 开启；子智能体 `memory=False`。
- 检索自实现、零新依赖；记忆/技能目录 `.code_agent/` 由所有者直写，`read_file`/`write_file` 仍拒。
- `remember`/`recall`/`create_skill` 走 policy（非 bypass）；`use_skill`/`dispatch_subagent` 语义不变。
- 既有测试不触发额外 LLM 调用（memory 默认关）；本迭代完成后全量回归全绿。
- 凭据不入库；提交前 `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"` 无命中。
- 无无关重构；只改本 spec（docs/superpowers/specs/2026-09-01-project-memory-skill-design.md）覆盖的文件。

---

### Task 1: memory.py MemoryStore

**Files:**
- Create: `code_agent/code_agent/memory.py`
- Test: `code_agent/tests/test_memory.py`（新建）

**Interfaces:**
- Produces:
  - `MemoryEntry(id, content, tags, source_session, created_at, updated_at, usage_count)` dataclass。
  - `MemoryStore(root)`：`all()` / `add(content, tags=None, source_session="") -> MemoryEntry` / `recall(query, top_k=3) -> list[MemoryEntry]`。
  - `_tokens(text)` / `_score(entry, query_tokens)`（模块级纯函数）。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_memory.py`）

```python
import os

import pytest

from code_agent.memory import MemoryStore, _tokens


def test_add_and_all(tmp_path):
    store = MemoryStore(str(tmp_path))
    e = store.add("the project uses uv for the environment", source_session="s1")
    assert e.id.startswith("code_agent-mem-")
    assert store.all() == [e]


def test_recall_relevant(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("the project uses uv for the environment")
    store.add("the api key lives in .env")
    hits = store.recall("uv environment", top_k=1)
    assert len(hits) == 1
    assert "uv" in hits[0].content


def test_recall_no_match_empty(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("python memory")
    assert store.recall("kafka", top_k=3) == []


def test_recall_bumps_usage_count(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("uv for the environment")
    store.recall("uv")
    assert store.all()[0].usage_count == 1


def test_recall_persists_meta(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("uv for the environment", tags=["env", "uv"], source_session="s1")
    store2 = MemoryStore(str(tmp_path))
    hits = store2.recall("uv")
    assert len(hits) == 1
    assert hits[0].source_session == "s1"
    assert hits[0].tags == ["env", "uv"]


def test_tokens_cjk_and_english():
    toks = _tokens("你好 world_1")
    assert "world_1" in toks
    assert "你" in toks and "好" in toks


def test_corrupt_lines_skipped(tmp_path):
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), "memories.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"id":"a","content":"ok"}\n')
        f.write("garbage-line\n")
        f.write('{"content":"bad tags","tags":"nope"}\n')
    store = MemoryStore(str(tmp_path))
    assert len(store.all()) == 2
    assert store.all()[1].tags == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_memory.py -v`
Expected: `ModuleNotFoundError: No module named 'code_agent.memory'`。

- [ ] **Step 3: 实现**（新建 `code_agent/memory.py`）

```python
"""Project memory: cross-session knowledge store with keyword retrieval."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

_MEM_ID_PATTERN = "code_agent-mem-%Y%m%d-%H%M%S%f"
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    source_session: str = ""
    created_at: str = ""
    updated_at: str = ""
    usage_count: int = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _make_id() -> str:
    return datetime.now().strftime(_MEM_ID_PATTERN)


def _tokens(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    cjk = [ch for ch in text if _CJK_RE.match(ch)]
    return words + cjk


def _score(entry: MemoryEntry, query_tokens: list[str]) -> int:
    entry_tokens = set(_tokens(entry.content))
    if not entry_tokens:
        return 0
    return sum(len(t) for t in query_tokens if t in entry_tokens)


class MemoryStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self._entries: list[MemoryEntry] = []
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, "memories.jsonl")

    def _load(self) -> None:
        if not os.path.isfile(self._path()):
            return
        try:
            with open(self._path(), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or "content" not in obj:
                        continue
                    tags = obj.get("tags", []) or []
                    if not isinstance(tags, list):
                        tags = []
                    usage_count = obj.get("usage_count", 0)
                    if not isinstance(usage_count, int):
                        usage_count = 0
                    self._entries.append(
                        MemoryEntry(
                            id=obj.get("id", ""),
                            content=obj["content"],
                            tags=[str(t) for t in tags],
                            source_session=obj.get("source_session", "") or "",
                            created_at=obj.get("created_at", "") or "",
                            updated_at=obj.get("updated_at", "") or "",
                            usage_count=usage_count,
                        )
                    )
        except OSError:
            pass

    def _save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(
                    json.dumps(
                        {
                            "id": e.id,
                            "content": e.content,
                            "tags": e.tags,
                            "source_session": e.source_session,
                            "created_at": e.created_at,
                            "updated_at": e.updated_at,
                            "usage_count": e.usage_count,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.replace(tmp, self._path())

    def all(self) -> list[MemoryEntry]:
        return list(self._entries)

    def add(self, content: str, tags: list[str] | None = None, source_session: str = "") -> MemoryEntry:
        now = _now()
        entry = MemoryEntry(
            id=_make_id(),
            content=content,
            tags=list(tags or []),
            source_session=source_session,
            created_at=now,
            updated_at=now,
        )
        self._entries.append(entry)
        self._save()
        return entry

    def recall(self, query: str, top_k: int = 3) -> list[MemoryEntry]:
        query_tokens = _tokens(query)
        scored = [(_score(e, query_tokens), e) for e in self._entries]
        hits = [(s, e) for s, e in scored if s > 0]
        hits.sort(key=lambda pair: (-pair[0], -pair[1].usage_count))
        selected = [e for _, e in hits[:top_k]]
        if selected:
            for e in selected:
                e.usage_count += 1
            self._save()
        return selected
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_memory.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/memory.py tests/test_memory.py
git commit -m "feat: MemoryStore 项目记忆库（JSONL + 关键词检索，Task 1/6）"
```

---

### Task 2: SkillRegistry.add

**Files:**
- Modify: `code_agent/code_agent/skills.py`
- Test: `code_agent/tests/test_skills.py`

**Interfaces:**
- Consumes: 既有 `SkillRegistry._project_dir`。
- Produces: `SkillRegistry.add(name, description, content) -> str`（非法 name 抛 `ValueError`）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_skills.py`）

```python
def test_skill_registry_add(tmp_path):
    import os
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    path = reg.add("build", "build and test", "1. run uv sync\n2. run uv run pytest")
    assert os.path.isfile(path)
    names = [s.name for s in reg.scan()]
    assert "build" in names
    content = reg.load("build")
    assert "uv run pytest" in content


def test_skill_registry_add_invalid_name(tmp_path):
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    with pytest.raises(ValueError):
        reg.add("../evil", "d", "c")
    with pytest.raises(ValueError):
        reg.add("has space", "d", "c")


def test_skill_registry_add_overwrites(tmp_path):
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    reg.add("build", "v1", "old")
    reg.add("build", "v2", "new")
    assert "new" in reg.load("build")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_skills.py -v`
Expected: `AttributeError: 'SkillRegistry' object has no attribute 'add'`。

- [ ] **Step 3: 实现**

`skills.py`：顶部 `import re` 追加到 `import os` 之后。在 `load` 方法之后追加：

```python
    def add(self, name: str, description: str, content: str) -> str:
        if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
            raise ValueError(f"invalid skill name: {name!r}")
        directory = os.path.join(self._project_dir, name)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "SKILL.md")
        desc_line = " ".join(description.split())
        text = f"---\nname: {name}\ndescription: {desc_line}\n---\n{content}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_skills.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/skills.py tests/test_skills.py
git commit -m "feat: SkillRegistry.add 程序化沉淀技能（Task 2/6）"
```

---

### Task 3: agent.py 记忆工具 + 注入 + 自动沉淀

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Test: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `MemoryStore`（Task 1）、`SkillRegistry.add`（Task 2）、`Tool`/`ToolRegistry`/`truncate`/`ToolResult`（既有）。
- Produces:
  - `RememberTool`/`RecallTool`/`CreateSkillTool`（session-bound，`bypass_policy=False`，`visible=self.memory`）。
  - `AgentSession(memory=False)`；`self._memory`/`self._memory_injected`；`_remember`/`_recall`/`_create_skill`。
  - `run_task` 重构：`_inject_memory`（首任务注入）+ `_run_loop`（原循环体）+ `_persist`（原 finally）+ `_auto_memorize`（成功结束自动总结）。
  - `new_session`/`load_session` 重置 `_memory_injected=False`；`_dispatch_subagent` 子会话 `memory=False`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_agent.py`）

```python
def test_memory_default_off_no_memory_tools(workdir):
    s = AgentSession(workdir=workdir, llm=object())
    assert s._registry.get("remember") is None
    assert s._registry.get("recall") is None
    assert s._registry.get("create_skill") is None


def test_memory_tools_present_when_enabled(workdir):
    s = AgentSession(workdir=workdir, llm=object(), memory=True)
    assert s._registry.get("remember") is not None
    assert s._registry.get("recall") is not None
    assert s._registry.get("create_skill") is not None
    names = {t["function"]["name"] for t in s._registry.schemas()}
    assert {"remember", "recall", "create_skill"} <= names


def test_remember_recall_tools(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object(), memory=True)
    r = s._run_tool(ToolCall(id="c1", name="remember", arguments={"content": "the project uses uv"}))
    assert r.ok is True
    r2 = s._run_tool(ToolCall(id="c2", name="recall", arguments={"query": "uv"}))
    assert r2.ok is True and "uv" in r2.output


def test_create_skill_tool(workdir):
    from code_agent.llm import ToolCall
    from code_agent.skills import SkillRegistry
    reg = SkillRegistry(workdir)
    s = AgentSession(workdir=workdir, llm=object(), skills=reg, memory=True)
    r = s._run_tool(ToolCall(id="c1", name="create_skill", arguments={
        "name": "build", "description": "build", "content": "run pytest"}))
    assert r.ok is True
    assert reg.load("build") is not None


def test_remember_when_disabled(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object())
    r = s._run_tool(ToolCall(id="c1", name="remember", arguments={"content": "x"}))
    assert r.ok is False and "unknown tool" in r.output


def test_memory_auto_inject(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s._memory.add("the project uses uv for the environment")
    s.run_task("set up the uv environment")
    assert any(
        "[Project memory]" in str(m.get("content", "")) for m in s.conversation.messages
        if m["role"] == "system"
    )


def test_memory_inject_once(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s._memory.add("uv environment")
    s.run_task("uv task one")
    s.run_task("uv task two")
    count = sum(1 for m in s.conversation.messages
                if "[Project memory]" in str(m.get("content", "")))
    assert count == 1


def test_memory_auto_memorize_on_success(workdir):
    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            last = str(messages[-1]["content"])
            if "Extract 1-3" in last:
                return LLMResponse(content='["the project builds with uv"]', tool_calls=[])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True)
    s.run_task("do something")
    assert any("the project builds with uv" in e.content for e in s._memory.all())


def test_memory_auto_memorize_skipped_on_failure(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n <= 3:
                return LLMResponse(content="", tool_calls=[ToolCall(id=f"c{self.n}", name="nonexistent", arguments={})])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), memory=True, max_iterations=5)
    res = s.run_task("boom")
    assert res.finished is False
    assert s._memory.all() == []


def test_subagent_memory_disabled(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0
            self.tools_calls = []

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            self.tools_calls.append([t["function"]["name"] for t in (tools or [])])
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub"})])
            if self.n == 2:
                return LLMResponse(content="sub done", tool_calls=[])
            return LLMResponse(content="[]", tool_calls=[])  # parent auto-summary

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, memory=True)
    s.run_task("parent task")
    sub_tools = set(llm.tools_calls[1])
    assert "dispatch_subagent" not in sub_tools
    assert not ({"remember", "recall", "create_skill"} & sub_tools)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 新用例失败（`memory` 参数不存在、记忆工具未注册、注入/自动总结未实现）。

- [ ] **Step 3: 实现**

`agent.py`：

1. import：在 `import json` 之后加 `import os`；在 `from code_agent.llm import LLMError, Usage` 之后追加 `from code_agent.memory import MemoryStore`。

2. `AgentSession.__init__` 签名在 `context_window` 参数后追加：

```python
        context_window: int | None = None,
        memory: bool = False,
    ) -> None:
```

在 `self.allow_subagent = allow_subagent` 附近追加：

```python
        self.memory = memory
        self._memory = MemoryStore(os.path.join(workdir, ".code_agent", "memory")) if memory else None
        self._memory_injected = False
```

在 registry 构建的 `tools.append(...)` 两行之后追加：

```python
        tools.append(RememberTool(self, visible=self.memory))
        tools.append(RecallTool(self, visible=self.memory))
        tools.append(CreateSkillTool(self, visible=self.memory))
```

3. 在 `DispatchSubagentTool` 类之后追加三个记忆工具类：

```python
class RememberTool(Tool):
    name = "remember"
    description = "Save a durable piece of project knowledge to the project memory for future sessions."
    parameters = {
        "content": {"type": "string", "description": "The knowledge/fact/gotcha to remember"},
        "tags": {"type": "string", "description": "Comma-separated tags (optional)"},
    }
    required = ["content"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._remember(args)


class RecallTool(Tool):
    name = "recall"
    description = "Search the project memory for relevant knowledge from prior sessions."
    parameters = {
        "query": {"type": "string", "description": "What to search for"},
        "top_k": {"type": "integer", "description": "Max results (default 3)"},
    }
    required = ["query"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._recall(args)


class CreateSkillTool(Tool):
    name = "create_skill"
    description = "Save a reusable workflow as a project skill (SKILL.md). It becomes available via use_skill in future sessions."
    parameters = {
        "name": {"type": "string", "description": "Skill name (letters, digits, - and _)"},
        "description": {"type": "string", "description": "Short description"},
        "content": {"type": "string", "description": "Markdown instructions body"},
    }
    required = ["name", "description", "content"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._create_skill(args)
```

4. `run_task` 重构。把原 `run_task`（`self.conversation.add_user(task)` 起、`finally` 持久化结束的整个方法体）拆为：

```python
    def run_task(
        self,
        task: str,
        on_delta: Callable[[str], None] | None = None,
        on_tool: Callable[[str, ToolResult], None] | None = None,
        on_assistant_start: Callable[[], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_stats: Callable[[Usage], None] | None = None,
    ) -> RunResult:
        self.conversation.add_user(task)
        self._inject_memory(task)
        self._on_delta = on_delta
        result: RunResult | None = None
        try:
            result = self._run_loop(on_delta, on_tool, on_assistant_start, on_tool_start, on_stats)
        finally:
            self._persist()
        if result is not None and self._memory is not None and result.finished:
            self._auto_memorize()
        return result

    def _run_loop(
        self,
        on_delta: Callable[[str], None] | None,
        on_tool: Callable[[str, ToolResult], None] | None,
        on_assistant_start: Callable[[], None] | None,
        on_tool_start: Callable[[str, dict], None] | None,
        on_stats: Callable[[Usage], None] | None,
    ) -> RunResult:
        """原 run_task 的 for 循环体（含内层 LLMError 处理与全部 return），去掉外层 try/finally。"""
        consecutive_failures = 0
        llm_error_count = 0
        for iteration in range(1, self.max_iterations + 1):
            if self.debug:
                print(f"[agent] iteration {iteration}")
            messages = self.conversation.build_messages(self.max_context_tokens)
            try:
                tools = self._registry.schemas()
                if on_assistant_start is not None:
                    on_assistant_start()
                response = self.llm.chat(messages, tools=tools, on_delta=on_delta)
            except LLMError as e:
                llm_error_count += 1
                if llm_error_count >= MAX_CONSECUTIVE_FAILURES:
                    return RunResult(final_text="", iterations=iteration, finished=False, reason=f"llm error: {e}")
                self.conversation.add_user(
                    f"[system] An LLM error occurred: {e}. "
                    "Please reply in plain text without tool calls, or continue if possible."
                )
                continue
            llm_error_count = 0
            if response.usage is not None:
                usage = response.usage
            else:
                usage = Usage(
                    prompt_tokens=sum(
                        estimate_tokens(str(m.get("content", ""))) for m in messages
                    ),
                    heuristic=True,
                )
            self.last_usage = usage
            if on_stats is not None:
                on_stats(usage)
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
                if on_tool_start is not None:
                    on_tool_start(tc.name, tc.arguments)
                result = self._run_tool(tc)
                if on_tool is not None:
                    on_tool(tc.name, result)
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

    def _persist(self) -> None:
        if self.store is not None:
            title = self._title()
            try:
                if self.session_id is None:
                    self.session_id = self.store.create(title)
                self.store.save(self.session_id, self.conversation.messages, title=title)
                if self.workspace is not None:
                    self.workspace.touch_session(self.session_id)
            except (OSError, ValueError) as e:
                print(f"[agent] warning: failed to persist session/workspace: {e}", file=sys.stderr)

    def _inject_memory(self, task: str) -> None:
        if self._memory is None or self._memory_injected:
            return
        self._memory_injected = True
        entries = self._memory.recall(task, top_k=3)
        if not entries:
            return
        lines = "\n".join(f"- {e.content}" for e in entries)
        self.conversation.add_system(
            "[Project memory]\nPrior sessions recorded about this project (use recall for more; remember to save new knowledge):\n"
            + lines
        )

    def _auto_memorize(self) -> None:
        try:
            resp = self.llm.chat(
                [
                    {"role": "system", "content": "You are an agent that just finished a task in a coding project."},
                    {
                        "role": "user",
                        "content": "Extract 1-3 durable, reusable pieces of project knowledge from this conversation "
                        "(facts, decisions, gotchas, key file locations). Reply with a JSON array of strings only.\n\n"
                        + self._memory_transcript(),
                    },
                ]
            )
            content = resp.content.strip()
            try:
                items = json.loads(content)
                entries = [str(i) for i in items if isinstance(i, str)] if isinstance(items, list) else []
            except json.JSONDecodeError:
                entries = [content]
            for e in entries:
                if e.strip():
                    self._memory.add(e.strip(), source_session=self.session_id or "")
        except Exception:  # noqa: BLE001 - auto-memorize must never break the loop
            pass

    def _memory_transcript(self) -> str:
        parts = []
        for m in self.conversation.messages[-40:]:
            role = m.get("role", "")
            text = str(m.get("content", ""))[:200]
            parts.append(f"{role}: {text}")
        return "\n".join(parts)[:6000]
```

5. `new_session` / `load_session` 末尾追加 `self._memory_injected = False`。

6. `_dispatch_subagent` 的子会话构造追加 `memory=False,`。

7. 在 `_use_skill` 附近追加 `_remember` / `_recall` / `_create_skill` 方法：

```python
    def _remember(self, arguments: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, output="memory is disabled")
        if not isinstance(arguments, dict):
            return ToolResult(ok=False, output="arguments must be an object")
        content = str(arguments.get("content", ""))
        if not content.strip():
            return ToolResult(ok=False, output="content is required")
        tags = [t.strip() for t in str(arguments.get("tags", "")).split(",") if t.strip()]
        entry = self._memory.add(content, tags=tags, source_session=self.session_id or "")
        return ToolResult(ok=True, output=f"remembered: {entry.id}")

    def _recall(self, arguments: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, output="memory is disabled")
        query = str(arguments.get("query", ""))
        if not query.strip():
            return ToolResult(ok=False, output="query is required")
        top_k = arguments.get("top_k", 3)
        if not isinstance(top_k, int) or top_k < 1:
            top_k = 3
        top_k = min(top_k, 10)
        entries = self._memory.recall(query, top_k=top_k)
        if not entries:
            return ToolResult(ok=True, output="(no relevant memories)")
        lines = [
            f"{i}. {e.content}" + (f" [tags: {', '.join(e.tags)}]" if e.tags else "")
            for i, e in enumerate(entries, 1)
        ]
        out, truncated = truncate("\n".join(lines))
        return ToolResult(ok=True, output=out, truncated=truncated)

    def _create_skill(self, arguments: dict) -> ToolResult:
        if self.skills is None:
            return ToolResult(ok=False, output="skills are not available")
        name = str(arguments.get("name", ""))
        description = str(arguments.get("description", ""))
        content = str(arguments.get("content", ""))
        if not name or not content:
            return ToolResult(ok=False, output="name and content are required")
        try:
            path = self.skills.add(name, description, content)
        except ValueError as e:
            return ToolResult(ok=False, output=f"invalid skill name: {e}")
        return ToolResult(ok=True, output=f"created skill: {name} ({path})")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py tests/test_memory.py tests/test_skills.py -v`
Expected: 全部 PASS（含既有 agent 用例——`memory` 默认关不改变行为）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: 项目记忆工具 + 首任务注入 + 成功自动沉淀（Task 3/6）"
```

---

### Task 4: cli.py 开启 memory

**Files:**
- Modify: `code_agent/code_agent/cli.py`
- Test: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `AgentSession(memory=...)`（Task 3）。
- Produces: `main()` 构造 `AgentSession(... memory=True)`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_cli.py`）

```python
def test_main_passes_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path)])
    assert rc == 0
    assert captured.get("memory") is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: `assert captured.get("memory") is True` 失败（None）。

- [ ] **Step 3: 实现**

`cli.py` 的 `AgentSession(...)` 构造中 `context_window=window,` 之后追加：

```python
            memory=True,
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: CLI 开启项目记忆（memory=True，Task 4/6）"
```

---

### Task 5: 文档同步 + ADR-026

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/tools.md`
- Modify: `code_agent/docs/context-management.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（ADR-026，不入库）

**Interfaces:** Consumes: Task 1-4 实际接口。

- [ ] **Step 1: 更新 `code_agent/docs/architecture.md`**

- 模块表加 `memory.py`（MemoryStore：JSONL + 关键词检索）。
- `agent.py` 行：`memory` 参数、`RememberTool`/`RecallTool`/`CreateSkillTool`、`_inject_memory`/`_auto_memorize`/`_run_loop`/`_persist`。
- `skills.py` 行：`add`。
- `cli.py` 行：`memory=True`。
- §2 数据流：首任务注入 + 成功自动沉淀两句。

- [ ] **Step 2: 更新 `code_agent/docs/tools.md`**

- §1 工具数量 10 → 13（模型可见：9 stateless + dispatch_subagent + use_skill + remember/recall/create_skill，按条件）。
- 新增 §3.11 remember / §3.12 recall / §3.13 create_skill（参数/返回/与 skill 机制关系/受保护路径由所有者直写）。

- [ ] **Step 3: 更新 `code_agent/docs/context-management.md`**

- 追加一节：项目记忆自动注入——首任务按相关性注入 top-K（≤3 条）system 块，限量防爆上下文；运行中 `recall` 按需召回。

- [ ] **Step 4: 更新 `code_agent/docs/design.md`**

§6 勾选追加：

```
- [x] 项目记忆与经验沉淀：remember/recall/create_skill + 首任务自动注入 top-K 记忆 + 成功自动总结沉淀（关键词检索零依赖，ADR-026）
```

§8 追加：`24. [x] 迭代增强：项目记忆与经验沉淀（ADR-026，设计见 docs/superpowers/specs/2026-09-01-project-memory-skill-design.md）`。测试计数按实际更新。

- [ ] **Step 5: 更新 `code_agent/docs/development.md`**

- §3 测试目录：`test_memory.py` 行新增说明；`test_skills.py` 补 add；`test_agent.py` 补记忆注入/沉淀用例。
- §2 CLI/TUI 说明：记忆开启说明（`--prompt`/`--interactive` 均开启）。

- [ ] **Step 6: 追加 ADR-026 到 `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`**（`## 后续决策记录处` 之前插入）

```markdown
## ADR-026：项目记忆与经验沉淀（agent 原生长期记忆）
- **日期**：2026-09-01
- **状态**：已实施
- **背景**：agent 无跨会话记忆，长文档靠模型手动 grep/分段读，无检索增强；用户要求"知识库/RAG 记忆系统"级别的特色功能。
- **决策**：①`MemoryStore`（`<workdir>/.code_agent/memory/`，JSONL + 自实现关键词相关度打分，零新依赖）；②三个 session-bound 工具 `remember`/`recall`/`create_skill`（走 policy；`create_skill` 写项目级 SKILL.md，复用 SkillRegistry）；③首任务到达时按任务相关性自动注入 top-K（≤3 条）system 记忆块；④任务成功结束（`finished=True`）用一次 LLM 调用把项目关键知识总结写入记忆（try/except 静默）；技能沉淀仅由模型显式 `create_skill` 触发；⑤`AgentSession(memory=False)` 默认关（保测试），`cli.main` 开启，子智能体关闭。
- **理由**：比通用 RAG 更贴合 coding agent（记忆带来源、可写可查、自动注入 + 模型主动召回双通道）；零依赖符合红线；默认关避免破坏既有测试与引入额外 LLM 调用。
- **影响**：新增 memory.py；agent/skills/cli 三处改动；工具增至 13（模型可见按条件）；345+ 测试全绿。
```

- [ ] **Step 7: 提交**

```bash
git add docs/architecture.md docs/tools.md docs/context-management.md docs/design.md docs/development.md
git commit -m "docs: 同步项目记忆/经验沉淀文档并记录 ADR-026"
```

---

### Task 6: 全量回归 + 凭据复核

**Files:** 无代码改动。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（345 + 本迭代新增 ≈ 365 用例）。

- [ ] **Step 2: CLI 冒烟**

Run: `uv run python -m code_agent --help`
Expected: 正常输出。

- [ ] **Step 3: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中。

- [ ] **Step 4: 收尾**

```bash
git status
git log --oneline -8
```
