# 工具层 Command + Registry 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把工具系统从 `_HANDLERS` 字典 + `if` 特判重构为显式 `Tool`（Command）+ `ToolRegistry`，统一分派，消除 `_run_tool` 特判。

**Architecture:** `tools.py` 新增 `Tool` 基类（name/description/parameters/required/bypass_policy/visible + schema()/validate()/execute()）与 `ToolRegistry`（register/get/schemas/execute）；9 个 stateless 工具各成 `Tool` 子类（包装既有 `_read_file` 等 handler）；`TOOL_SCHEMAS`/`execute()` 顶层导出保留（由默认 registry 派生）。`agent.py` 把 `use_skill`/`dispatch_subagent` 变为 session-bound `Tool` 子类进 registry，`_run_tool` 统一为 registry 分派 + policy（bypass 除外）+ execute。

**Tech Stack:** Python 3.11+，pytest（离线测试）。

## Global Constraints

- 所有命令在 `code_agent/` 目录内经 `uv run` 执行。
- 保留顶层导出 `execute(name, args, workdir)` 与 `TOOL_SCHEMAS`（由默认 registry 派生）；`tests/test_tools.py` 现有用例全部不破；重构后 **328+ 用例全绿**。
- 行为保留（必须逐项成立）：`execute()` 的 "unknown tool: {name}" 与 "tool crashed: {type}: {err}" 消息不变；`dispatch_subagent`/`use_skill` 不经过 policy；`allow_subagent=False` 时 `dispatch_subagent` schema 不可见且运行时仍返回 "subagent dispatch is disabled for subagents"；`skills=None` 时 `use_skill` schema 不可见。
- 凭据不入库；提交前 `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"` 无命中。
- 无无关重构；只改本 spec（docs/superpowers/specs/2026-09-01-tool-command-refactor-design.md）覆盖的文件。

---

### Task 1: tools.py Tool 基类 + ToolRegistry + 9 个子类

**Files:**
- Modify: `code_agent/code_agent/tools.py`
- Test: `code_agent/tests/test_tools.py`

**Interfaces:**
- Consumes: 既有 `ToolResult`/`truncate`/`_schema`/`_read_file`/`_list_dir`/`_write_file`/`_edit_file`/`_run_command`/`_glob`/`_grep`/`_web_fetch`/`_web_search`。
- Produces:
  - `class Tool`：`name`/`description`/`parameters`/`required`/`bypass_policy=False`/`visible=True`；`schema() -> dict`；`validate(args) -> str | None`（默认 None）；`execute(args, workdir) -> ToolResult`（默认 `raise NotImplementedError`）。
  - `class ToolRegistry`：`register(tool)` / `get(name) -> Tool | None` / `schemas() -> list[dict]`（仅 visible）/ `execute(name, args, workdir) -> ToolResult`（unknown → `f"unknown tool: {name}"`；validate 消息；异常 → `f"tool crashed: {type(e).__name__}: {e}"`）。
  - 9 个 `Tool` 子类：`ReadFileTool`/`ListDirTool`/`WriteFileTool`/`EditFileTool`/`RunCommandTool`/`GlobTool`/`GrepTool`/`WebFetchTool`/`WebSearchTool`。
  - `BASE_TOOLS: list[Tool]`（顺序同现有 `TOOL_SCHEMAS`）；`_DEFAULT_REGISTRY`；保留 `TOOL_SCHEMAS = [t.schema() for t in BASE_TOOLS]`；保留顶层 `execute(name, args, workdir)`。

- [ ] **Step 1: 写失败测试**（把 `tests/test_tools.py` 顶部 `from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute, truncate` 替换为下面的 import，并在文件末尾追加新用例）

```python
from code_agent.tools import (
    TOOL_SCHEMAS,
    BASE_TOOLS,
    Tool,
    ToolRegistry,
    ToolResult,
    ReadFileTool,
    execute,
    truncate,
)


def test_tool_schema_roundtrip():
    s = ReadFileTool().schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "read_file"
    assert set(s["function"]["parameters"]["required"]) == {"path"}


def test_base_tools_nine_and_schema_names():
    assert len(BASE_TOOLS) == 9
    names = {t.name for t in BASE_TOOLS}
    assert names == {"read_file", "write_file", "edit_file", "list_dir",
                     "run_command", "glob", "grep", "web_fetch", "web_search"}
    assert names == {s["function"]["name"] for s in TOOL_SCHEMAS}


def test_registry_register_and_get():
    reg = ToolRegistry()
    t = ReadFileTool()
    reg.register(t)
    assert reg.get("read_file") is t
    assert reg.get("nope") is None


def test_registry_schemas_filters_invisible():
    class _Hidden(Tool):
        name = "hidden"
        description = "x"
        parameters = {}
        required = []
        visible = False

    reg = ToolRegistry([_Hidden()])
    assert reg.schemas() == []


def test_registry_execute_unknown():
    assert execute("nope", {}, "/tmp").ok is False
    assert "unknown tool: nope" in execute("nope", {}, "/tmp").output


def test_registry_execute_delegates_to_tool():
    reg = ToolRegistry([ReadFileTool()])
    r = reg.execute("read_file", {"path": "missing"}, "/tmp")
    assert r.ok is False and "not found" in r.output


def test_registry_execute_validate_hook():
    class _Guard(Tool):
        name = "guard"
        description = ""
        parameters = {}
        required = []

        def validate(self, args):
            return "bad arg" if "x" not in args else None

        def execute(self, args, workdir):
            return ToolResult(ok=True, output="ok")

    reg = ToolRegistry([_Guard()])
    assert reg.execute("guard", {}, "/tmp").ok is False
    assert reg.execute("guard", {}, "/tmp").output == "bad arg"
    assert reg.execute("guard", {"x": 1}, "/tmp").ok is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tools.py -v`
Expected: 新用例报 `ImportError`（`Tool`/`ToolRegistry`/`BASE_TOOLS`/`ReadFileTool` 不存在）。

- [ ] **Step 3: 实现**

`tools.py`：
1. 在 `_schema` 函数之后、`TOOL_SCHEMAS` 之前插入 `Tool` 基类与 `ToolRegistry`：

```python
class Tool:
    """Command 模式基类：一个工具 = schema 声明 + 本地执行。"""
    name: str = ""
    description: str = ""
    parameters: dict = {}
    required: list[str] = []
    bypass_policy: bool = False
    visible: bool = True

    def schema(self) -> dict:
        return _schema(self.name, self.description, self.parameters, self.required)

    def validate(self, args: dict) -> str | None:
        return None

    def execute(self, args: dict, workdir: str) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values() if t.visible]

    def execute(self, name: str, args: dict, workdir: str) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(ok=False, output=f"unknown tool: {name}")
        msg = tool.validate(args)
        if msg:
            return ToolResult(ok=False, output=msg)
        try:
            return tool.execute(args, workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")
```

2. 把现有 `TOOL_SCHEMAS` 常量替换为 9 个 `Tool` 子类（parameters/required 逐字迁移自现有 schema；execute 委托既有 handler）：

```python
class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file (with optional line range). Refuses protected paths."
    parameters = {
        "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
        "offset": {"type": "integer", "description": "1-based start line"},
        "limit": {"type": "integer", "description": "Max lines to read"},
    }
    required = ["path"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _read_file(args, workdir)


class ListDirTool(Tool):
    name = "list_dir"
    description = "List directory entries with type and size. Skips .git and caches."
    parameters = {"path": {"type": "string", "description": "Directory path (defaults to workdir)"}}
    required = []

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _list_dir(args, workdir)


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file (must be inside the workdir)."
    parameters = {
        "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
        "content": {"type": "string", "description": "Full file content"},
    }
    required = ["path", "content"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _write_file(args, workdir)


class EditFileTool(Tool):
    name = "edit_file"
    description = "Replace an exact substring in a file (must match uniquely unless replace_all)."
    parameters = {
        "path": {"type": "string", "description": "File path"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
    }
    required = ["path", "old_string", "new_string"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _edit_file(args, workdir)


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run a shell command in the workdir (timeout and output limits apply)."
    parameters = {
        "command": {"type": "string", "description": "Shell command"},
        "timeout": {"type": "number", "description": f"Timeout in seconds (default {DEFAULT_COMMAND_TIMEOUT})"},
    }
    required = ["command"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _run_command(args, workdir)


class GlobTool(Tool):
    name = "glob"
    description = "Find files by glob pattern (supports ** recursion). Refuses protected paths."
    parameters = {
        "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
        "path": {"type": "string", "description": "Directory to search (defaults to workdir)"},
    }
    required = ["pattern"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _glob(args, workdir)


class GrepTool(Tool):
    name = "grep"
    description = "Search file contents with a regex. Skips .git, protected and gitignored paths."
    parameters = {
        "pattern": {"type": "string", "description": "Regex to search"},
        "path": {"type": "string", "description": "File or directory (defaults to workdir)"},
        "include": {"type": "string", "description": "fnmatch on filename, e.g. '*.py'"},
        "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
        "output_mode": {
            "type": "string",
            "enum": ["content", "files_with_matches", "count"],
            "description": "Default 'content'",
        },
    }
    required = ["pattern"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _grep(args, workdir)


class WebFetchTool(Tool):
    name = "web_fetch"
    description = ("Fetch a public web page (http/https) and return its title, readable text and first 10 links. "
                  "Refuses non-public addresses (internal/private networks, file://).")
    parameters = {"url": {"type": "string", "description": "Public http(s) URL to fetch"}}
    required = ["url"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _web_fetch(args, workdir)


class WebSearchTool(Tool):
    name = "web_search"
    description = ("Search the web (DuckDuckGo Lite, keyless). Returns numbered results with title, real URL and snippet. "
                  "Use web_fetch on a result URL for full content.")
    parameters = {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": "Max results (default 8)"},
    }
    required = ["query"]

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return _web_search(args, workdir)
```

3. 文件末尾把 `_HANDLERS` + `execute` 替换为：

```python
BASE_TOOLS: list[Tool] = [
    ReadFileTool(),
    ListDirTool(),
    WriteFileTool(),
    EditFileTool(),
    RunCommandTool(),
    GlobTool(),
    GrepTool(),
    WebFetchTool(),
    WebSearchTool(),
]

TOOL_SCHEMAS = [t.schema() for t in BASE_TOOLS]

_DEFAULT_REGISTRY = ToolRegistry(list(BASE_TOOLS))


def execute(name: str, args: dict, workdir: str) -> ToolResult:
    return _DEFAULT_REGISTRY.execute(name, args, workdir)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tools.py -v`
Expected: 全部 PASS（既有用例 + 新用例）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/tools.py tests/test_tools.py
git commit -m "refactor: 工具层 Command+Registry 显式化（Tool 基类 + ToolRegistry + 9 子类，ADR-025 前置，Task 1/3）"
```

---

### Task 2: agent.py session-bound 工具 + registry 接线

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Test: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `Tool`/`ToolRegistry`/`BASE_TOOLS`/`ToolResult`（Task 1）。
- Produces:
  - `UseSkillTool(Tool)`：`bypass_policy=True`；`__init__(session, *, visible)`；`execute` → `session._use_skill(args)`。
  - `DispatchSubagentTool(Tool)`：`bypass_policy=True`；`__init__(session, *, visible)`；`execute` 先判 `session.allow_subagent`（False → "subagent dispatch is disabled for subagents"），否则 `session._dispatch_subagent(args, on_delta=session._on_delta)`。
  - `AgentSession._registry`；`run_task` 注入 `self._registry.schemas()`；`_run_tool` 统一分派；删除 `_USE_SKILL_SCHEMA`/`_DISPATCH_SUBAGENT_SCHEMA`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_agent.py`）

```python
def test_run_task_tools_from_registry(workdir, tmp_path):
    from code_agent.llm import ToolCall
    from code_agent.skills import SkillRegistry
    import os as _os

    d = _os.path.join(str(tmp_path / "proj"), ".code_agent", "skills", "greeting")
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: say hi\n---\nhello\n")
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))

    class _LLM:
        def __init__(self):
            self.tools = None

        def chat(self, messages, tools=None, on_delta=None):
            self.tools = tools
            return LLMResponse(content="done", tool_calls=[])

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, skills=reg)
    s.run_task("task")
    names = {t["function"]["name"] for t in llm.tools}
    assert "read_file" in names
    assert "use_skill" in names
    assert "dispatch_subagent" in names


def test_run_task_hides_conditional_schemas(workdir):
    class _LLM:
        def __init__(self):
            self.tools = None

        def chat(self, messages, tools=None, on_delta=None):
            self.tools = tools
            return LLMResponse(content="done", tool_calls=[])

    llm = _LLM()
    s = AgentSession(workdir=workdir, llm=llm, allow_subagent=False)
    s.run_task("task")
    names = {t["function"]["name"] for t in llm.tools}
    assert "use_skill" not in names
    assert "dispatch_subagent" not in names


def test_run_tool_unknown(workdir):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="nonexistent", arguments={})])
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    s.run_task("task")
    assert any(
        m["role"] == "tool" and "unknown tool: nonexistent" in str(m.get("content", ""))
        for m in s.conversation.messages
    )


def test_run_tool_bypass_policy(workdir):
    from code_agent.llm import ToolCall
    from code_agent.permissions import Policy

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    policy = Policy(deny=["dispatch_subagent:*", "run_command:*"])
    s = AgentSession(workdir=workdir, llm=_LLM(), policy=policy)
    tc = ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "hi"})
    res = s._run_tool(tc)
    assert res.ok is True  # 编排工具绕过 deny
    tc2 = ToolCall(id="c2", name="run_command", arguments={"command": "ls"})
    res2 = s._run_tool(tc2)
    assert res2.ok is False and "permission denied" in res2.output


def test_run_tool_dispatch_disabled_message(workdir):
    from code_agent.llm import ToolCall
    s = AgentSession(workdir=workdir, llm=object(), allow_subagent=False)
    tc = ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "x"})
    res = s._run_tool(tc)
    assert res.ok is False and "subagent dispatch is disabled" in res.output
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 新用例失败（`_run_tool` 无 registry / 消息不符 / tools 注入仍是旧拼接）。

- [ ] **Step 3: 实现**

`agent.py`：
1. import 行 `from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute, truncate` 改为：

```python
from code_agent.tools import BASE_TOOLS, Tool, ToolRegistry, ToolResult, truncate
```

2. 删除 `_USE_SKILL_SCHEMA` 与 `_DISPATCH_SUBAGENT_SCHEMA` 两个常量，替换为两个 session-bound 工具类（放在 `_DISPATCH_SUBAGENT_SCHEMA` 原位置）：

```python
class UseSkillTool(Tool):
    name = "use_skill"
    description = "Load a skill's instructions into context. Returns the skill content; follow it."
    parameters = {"name": {"type": "string", "description": "Skill name"}}
    required = ["name"]
    bypass_policy = True

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._use_skill(args)


class DispatchSubagentTool(Tool):
    name = "dispatch_subagent"
    description = ("Delegate a sub-task to a subagent that runs its own agent loop with all tools "
                  "except subagent dispatch. Returns the subagent's final report.")
    parameters = {"task": {"type": "string", "description": "Sub-task description"}}
    required = ["task"]
    bypass_policy = True

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        if not self._session.allow_subagent:
            return ToolResult(ok=False, output="subagent dispatch is disabled for subagents")
        return self._session._dispatch_subagent(args, on_delta=self._session._on_delta)
```

3. `AgentSession.__init__`：在 `self.allow_subagent = allow_subagent` 与 `self.last_usage` 赋值之后、`self._system_prompt` 构建之前插入：

```python
        tools = list(BASE_TOOLS)
        tools.append(UseSkillTool(self, visible=self.skills is not None))
        tools.append(DispatchSubagentTool(self, visible=self.allow_subagent))
        self._registry = ToolRegistry(tools)
```

4. `run_task` 中 `tools = TOOL_SCHEMAS + ...` 两行替换为：

```python
                    tools = self._registry.schemas()
```

5. `_run_tool` 整体替换为：

```python
    def _run_tool(self, tc) -> ToolResult:
        tool = self._registry.get(tc.name)
        if tool is None:
            return ToolResult(ok=False, output=f"unknown tool: {tc.name}")
        if not tool.bypass_policy and self.policy is not None:
            result = self.policy.check(tc.name, tc.arguments, interact=self.interact, ask=self.ask)
            if result.decision == "deny":
                return ToolResult(ok=False, output=f"permission denied: {result.reason or tc.name}")
        try:
            return tool.execute(tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py tests/test_tools.py -v`
Expected: 全部 PASS（含既有 dispatch/skill/权限用例）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "refactor: use_skill/dispatch_subagent 进 registry，_run_tool 统一分派（Task 2/3）"
```

---

### Task 3: 文档同步 + ADR-025

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/tools.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（ADR-025，不入库）

**Interfaces:** Consumes: Task 1-2 实际接口。

- [ ] **Step 1: 更新 `code_agent/docs/architecture.md`**

- `tools.py` 行：`TOOL_SCHEMAS` 9 个工具 + `execute` → 改为 `Tool` 基类（Command：name/description/parameters/required/bypass_policy/visible + schema()/validate()/execute()）+ `ToolRegistry`（register/get/schemas/execute）+ 9 个 `Tool` 子类（`BASE_TOOLS`）；`TOOL_SCHEMAS`/`execute()` 由 `_DEFAULT_REGISTRY` 派生保留。
- `agent.py` 行：`use_skill`/`dispatch_subagent` 为 session-bound `Tool` 子类（`bypass_policy=True`），`AgentSession._registry` 条件注册（visible 控制 schema），`_run_tool` 统一分派（registry → policy（bypass 除外）→ execute）。
- §3 tools.py / agent.py 接口段：同步上述描述；`_run_tool` 特判消除。

- [ ] **Step 2: 更新 `code_agent/docs/tools.md`**

- §1：schema 权威源从 `TOOL_SCHEMAS` 改为 `Tool` 对象属性（`TOOL_SCHEMAS` 仍为派生导出）；`dispatch_subagent` schema 由 `DispatchSubagentTool` 提供。
- 新增工具机制说明：`Tool` 基类 + `ToolRegistry`，新增工具 = 实现 `Tool` 子类 + `register`。
- 保留：统一返回格式、各工具接口、dispatch_subagent 阉割/权限继承说明。

- [ ] **Step 3: 更新 `code_agent/docs/design.md`**

§6 功能范围勾选追加：

```
- [x] 工具层 Command+Registry 显式化（Tool 基类 + ToolRegistry，9 stateless + use_skill/dispatch_subagent session-bound，bypass_policy/visible 语义，ADR-025）
```

§8 开发路线追加：`23. [x] 迭代增强：工具层 Command+Registry 重构（ADR-025，设计见 docs/superpowers/specs/2026-09-01-tool-command-refactor-design.md）`。

- [ ] **Step 4: 更新 `code_agent/docs/development.md`**

- §3 测试目录：`test_tools.py` 行补充 registry/Command 用例（register/get/schemas 可见性/validate 钩子/unknown）。

- [ ] **Step 5: 追加 ADR-025 到 `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`**（`## 后续决策记录处` 之前插入）

```markdown
## ADR-025：工具层 Command + Registry 显式化
- **日期**：2026-09-01
- **状态**：已实施
- **背景**：工具系统命令模式是隐含的（`_HANDLERS` 字典 + `_run_tool` 对 use_skill/dispatch_subagent 的 if 特判），新增工具需同时改 schema 列表、handler 注册、循环层特判，扩展路径不统一。
- **决策**：新增 `Tool` 基类（Command：name/description/parameters/required/bypass_policy/visible + schema()/validate()/execute()）与 `ToolRegistry`（register/get/schemas/execute）；9 个 stateless 工具各成 `Tool` 子类（包装既有 handler）；`TOOL_SCHEMAS`/`execute()` 顶层导出保留（由默认 registry 派生）；`use_skill`/`dispatch_subagent` 变为 session-bound `Tool` 子类（`bypass_policy=True`），`AgentSession._registry` 条件注册（`visible` 控制 schema 注入），`_run_tool` 统一为 registry 分派 + policy（bypass 除外）+ execute。
- **理由**：命令 + 注册表 + 策略装饰权限是工具型 agent 的标准架构（Claude Code/OpenCode/LangChain 同构）；显式化后新增工具只实现一个类并注册，扩展路径单一；消除循环层特判。
- **影响**：tools.py/agent.py 两文件重构；行为保留（unknown/crash 消息、dispatch/use_skill 绕过 policy、阉割 visible+运行时拒绝）；328+ 测试全绿；架构/tools 文档同步。
```

- [ ] **Step 6: 提交**

```bash
git add docs/architecture.md docs/tools.md docs/design.md docs/development.md
git commit -m "docs: 同步工具层 Command+Registry 文档并记录 ADR-025"
```

---

### Task 4: 全量回归 + 凭据复核

**Files:** 无代码改动。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（328 + 本迭代新增 ≈ 340 用例）。

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
