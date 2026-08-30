# TUI 打磨实现计划（多轮渲染/布局重叠/子智能体状态/! 指令/移除命令面板）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Textual 8.2.8**：以安装版本实际 API 为准；若计划代码与 8.2.8 不符，实现者可最小适配并在报告中注明（如 `VerticalScroll`/`OptionList`/`run_test`/`scroll_to` 的类名与属性）。

**Goal:** 修复 TUI 两个 bug（多轮工具行错位、Ctrl+L 布局重叠），新增子智能体运行标注、`!` 终端命令，移除 Ctrl+P 命令面板。

**Architecture:** `agent.py` `run_task` 新增 `on_assistant_start`/`on_tool_start` 回调；`worker.py` 桥接；`app.py` 多轮渲染重构（按回合新建 assistant 行）、布局切换重排+钳制滚动、子智能体标注、`!` 命令、命令面板移除；`__init__.py` `_line_style` 补样式。

**Tech Stack:** Python 3.11+，`textual` 8.2.8，`pytest`。测试离线（FakeLLM/mock subprocess + Textual `run_test`）。

## Global Constraints

- 禁止任何 agent 框架/SDK；改动均在既有 `tui/` 包与 `agent.py` 内。
- `run_task(task, on_delta, on_tool, on_assistant_start=None, on_tool_start=None)`——新回调向后兼容（缺省 None），既有语义不变。
- 子智能体标注：不回显完整报告；`dispatch_subagent` 运行时显示"子智能体运行中…"，完成后"✓ 完成"。
- `!cmd`：shell 执行（`cwd=session.workdir`，超时 120s），回显 `$ cmd` + 输出 + `[exit <code>]`；busy 互斥（agent 或另一条 ! 运行时拒绝）；不走 agent/policy；不落会话持久化。
- `_line_style`：`[tool] use_skill` → 品红；`[tool] dispatch_subagent` → 青；`[subagent]` 行运行黄/完成绿。
- 命令面板移除：`COMMANDS = ()` + `Binding("ctrl+p", "noop", "disabled")`。
- 布局切换：`action_toggle_sessions` 后重渲染 `#log` body + 钳制滚动偏移。
- 凭据纪律不变；测试全部离线；每个任务结束必须测试通过并提交（不 rebase/改写历史）。

---

### Task 1: agent.py —— `on_assistant_start` / `on_tool_start` 回调

**Files:**
- Modify: `code_agent/code_agent/agent.py`（`run_task` 签名 + 两处触发）
- Modify: `code_agent/tests/test_agent.py`（回调用例）

**Interfaces:**
- Consumes: 无。
- Produces:
  - `run_task(self, task, on_delta=None, on_tool=None, on_assistant_start: Callable[[], None] | None = None, on_tool_start: Callable[[str], None] | None = None) -> RunResult`
  - 每次 `llm.chat` 前触发 `on_assistant_start()`；每个工具执行前触发 `on_tool_start(tc.name)`。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_agent.py`：

```python
def test_agent_on_assistant_and_tool_start_callbacks(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        LLMResponse(content="planning", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})]),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    starts, tool_starts = [], []
    result = session.run_task(
        "do it",
        on_assistant_start=lambda: starts.append(1),
        on_tool_start=lambda n: tool_starts.append(n),
    )
    assert result.finished and result.final_text == "done"
    assert len(starts) == 2
    assert tool_starts == ["read_file"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -k "on_assistant" -v`
Expected: FAIL（`TypeError: run_task() got an unexpected keyword argument 'on_assistant_start'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/agent.py` `run_task` 签名改为：

```python
    def run_task(
        self,
        task: str,
        on_delta: Callable[[str], None] | None = None,
        on_tool: Callable[[str, ToolResult], None] | None = None,
        on_assistant_start: Callable[[], None] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
    ) -> RunResult:
```

在 `response = self.llm.chat(messages, tools=tools, on_delta=on_delta)` 之前插入：

```python
                    if on_assistant_start is not None:
                        on_assistant_start()
```

在工具循环 `result = self._run_tool(tc)` 之前插入：

```python
                for tc in response.tool_calls:
                    if on_tool_start is not None:
                        on_tool_start(tc.name)
                    result = self._run_tool(tc)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py -k "on_assistant" -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿（252 + 1 = 253）

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: agent.py run_task 新增 on_assistant_start/on_tool_start 回调（Task 1/8）"
```

---

### Task 2: worker.py —— 桥接两个新回调

**Files:**
- Modify: `code_agent/code_agent/tui/worker.py`
- Modify: `code_agent/tests/test_tui_worker.py`

**Interfaces:**
- Consumes: Task 1 的 `run_task` 新参数。
- Produces: `AgentWorker.__init__(..., on_assistant_start=None, on_tool_start=None)`；`_run` 传给 `session.run_task(on_assistant_start=..., on_tool_start=...)`；内部 `_assistant_start()` / `_tool_start(name)` 经 `call_from_thread` 桥接。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tui_worker.py`：

```python
def test_worker_bridges_new_callbacks(workdir):
    from code_agent.llm import ToolCall
    from code_agent.tui.worker import AgentWorker

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})])
            return LLMResponse(content="done", tool_calls=[])

    import os
    open(os.path.join(workdir, "a.txt"), "w").write("hello")
    session = _make_session(workdir, _LLM())
    app = _FakeApp()
    starts, tool_starts = [], []

    w = AgentWorker(
        app, session,
        on_delta=lambda c: None,
        on_tool=lambda n, r: None,
        on_done=lambda r: None,
        on_assistant_start=lambda: starts.append(1),
        on_tool_start=lambda n: tool_starts.append(n),
    )
    w.start("hi")
    deadline = time.time() + 5
    while len(starts) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(starts) == 2
    assert tool_starts == ["read_file"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_worker.py -k "new_callbacks" -v`
Expected: FAIL（`TypeError: __init__() got an unexpected keyword argument 'on_assistant_start'`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/worker.py`：
1) `__init__` 签名追加 `on_assistant_start=None, on_tool_start=None`；存 `self._on_assistant_start` / `self._on_tool_start`。
2) `_run` 的 `run_task` 调用改为：
```python
            result = self.session.run_task(
                task,
                on_delta=self._delta,
                on_tool=self._tool,
                on_assistant_start=self._assistant_start,
                on_tool_start=self._tool_start,
            )
```
3) 追加方法：
```python
    def _assistant_start(self) -> None:
        self.app.call_from_thread(self._on_assistant_start)

    def _tool_start(self, name: str) -> None:
        self.app.call_from_thread(lambda: self._on_tool_start(name))
```
（`self._on_assistant_start` / `self._on_tool_start` 恒非 None——`run_task` 只在回调非 None 时才触发，`_run` 总是传入，故直接调用安全。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_worker.py -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/worker.py tests/test_tui_worker.py
git commit -m "feat: tui/worker.py 桥接 on_assistant_start/on_tool_start（Task 2/8）"
```

---

### Task 3: app.py —— 多轮渲染重构

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`
- Modify: `code_agent/tests/test_tui_app.py`

**Interfaces:**
- Consumes: Task 2 的 AgentWorker 新参数。
- Produces:
  - `_start_task` 不再预置 `"assistant: "` 行；AgentWorker 构造传 `on_assistant_start=self._on_assistant_start`。
  - `_on_assistant_start()`：`log.append("assistant: ")`；`self._assistant_idx = len(log._lines) - 1`。
  - `_on_delta` / `_on_tool` / `_on_done` 逻辑沿用（更新钉住行 / 追加工具行 / 完成时更新 assistant 行）。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tui_app.py`：

```python
def test_app_multi_round_order(workdir, tmp_path):
    from code_agent.llm import ToolCall
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                if on_delta:
                    on_delta("思考")
                return LLMResponse(content="思考", tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})])
            if on_delta:
                on_delta("结论")
            return LLMResponse(content="结论", tool_calls=[])

    app = _make_app(workdir, tmp_path, _LLM())

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "任务"
            await pilot.press("enter")
            for _ in range(150):
                log = app.query_one("#log")
                if "结论" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            lines = [l.plain for l in app.query_one("#log")._lines]
            assert lines[0].startswith("> user:")
            assert lines[1].startswith("assistant:") and "思考" in lines[1]
            assert lines[2].startswith("[tool] read_file")
            assert lines[3].startswith("assistant:") and "结论" in lines[3]
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

需要把 `test_tui_app.py` 现有的 `_make_app` 改造为接受可选 llm（默认真实 `_FakeLLM`）：
```python
def _make_app(workdir, tmp_path, llm=None):
    from code_agent.agent import AgentSession
    from code_agent.llm import LLMResponse
    from code_agent.session import SessionStore
    from code_agent.tui.app import CodeAgentApp
    if llm is None:
        class _FakeLLM:
            def chat(self, messages, tools=None, on_delta=None):
                if on_delta:
                    on_delta("答复内容")
                return LLMResponse(content="答复内容", tool_calls=[])
        llm = _FakeLLM()
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3)
    store = SessionStore(str(tmp_path / "sessions"))
    return CodeAgentApp(session, store, None, model="test")
```
（若现有 `_make_app` 结构不同，以实际为准适配；本 Task 的导入 `Path`/`LLMResponse` 需在文件内可用。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py::test_app_multi_round_order -v`
Expected: FAIL（顺序断言不符——当前多轮合并第一行）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/app.py`：
1) `_start_task` 中删除 `log.append("assistant: ")` 与 `self._assistant_idx = len(log._lines) - 1`；AgentWorker 构造加 `on_assistant_start=self._on_assistant_start, on_tool_start=self._on_tool_start`：
```python
        self._worker = AgentWorker(
            self, self.session,
            on_delta=lambda c: self._on_delta(c),
            on_tool=lambda n, r: self._on_tool(n, r),
            on_done=lambda r: self._on_done(r),
            on_ask=lambda p, resp: self._on_ask(p, resp),
            on_ask_timeout=lambda: self._clear_ask(),
            on_assistant_start=self._on_assistant_start,
            on_tool_start=self._on_tool_start,
        )
```
2) 新增方法：
```python
    def _on_assistant_start(self) -> None:
        log = self.query_one("#log", ConversationLog)
        log.append("assistant: ")
        self._assistant_idx = len(log._lines) - 1
```
3) `_on_delta` 维持钉住行更新；`_on_tool` 追加工具行；`_on_done` 更新当前 assistant 行 + stop/session 行。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py -v`
Expected: PASS（含多轮顺序用例）
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/app.py tests/test_tui_app.py
git commit -m "feat: app.py 多轮渲染（按回合新建 assistant 行，Task 3/8）"
```

---

### Task 4: app.py —— 子智能体运行标注 + `_line_style` 样式

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`（`_on_tool_start` / `_on_tool` 子智能体分支）
- Modify: `code_agent/code_agent/tui/__init__.py`（`_line_style` 补 `[tool] use_skill` / `[tool] dispatch_subagent` / `[subagent]` 样式）
- Modify: `code_agent/tests/test_tui.py`（样式用例）
- Modify: `code_agent/tests/test_tui_app.py`（标注用例）

**Interfaces:**
- Consumes: Task 2 的 `on_tool_start` 桥。
- Produces:
  - `self._subagent_idx: int | None = None`（__init__ 初始化）。
  - `_on_tool_start(name)`：`name == "dispatch_subagent"` → `log.append("[subagent] 子智能体运行中…")`；`self._subagent_idx = len(log._lines) - 1`。
  - `_on_tool(name, res)`：`name == "dispatch_subagent"` 且 `_subagent_idx` 有效 → `log.update_line(_subagent_idx, "[subagent] ✓ 完成")`。
  - `_line_style`：`[tool]` 内 `use_skill` → `"magenta"`；`dispatch_subagent` → `"bold cyan"`；`[subagent]` 行含 `✓` → `"green"` 否则 `"yellow"`。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tui.py`：

```python
def test_line_style_skill_and_subagent():
    assert _line_style("[tool] use_skill ok | ...") == "magenta"
    assert _line_style("[tool] dispatch_subagent ok | ...") == "bold cyan"


def test_line_style_subagent_running_and_done():
    assert _line_style("[subagent] 子智能体运行中…") == "yellow"
    assert _line_style("[subagent] ✓ 完成") == "green"
```

追加到 `code_agent/tests/test_tui_app.py`：

```python
def test_app_subagent_status_marker(workdir, tmp_path):
    from code_agent.llm import ToolCall

    class _LLM:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, on_delta=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(
                    content="", tool_calls=[ToolCall(id="c1", name="dispatch_subagent", arguments={"task": "sub"})],
                )
            return LLMResponse(content="done", tool_calls=[])

    app = _make_app(workdir, tmp_path, _LLM())

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hi"
            await pilot.press("enter")
            for _ in range(150):
                log = app.query_one("#log")
                text = "".join(l.plain for l in log._lines)
                if "[subagent] ✓ 完成" in text:
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "[subagent] 子智能体运行中…" in text or "[subagent] ✓ 完成" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```
（说明：`dispatch_subagent` 会派生子会话，子会话共享同一 `llm`（`_LLM`），故 `n` 计数：父 iter1→dispatch→子 iter1(n=2)→"done"→子完成回报告→父 on_tool→"✓ 完成"；父 iter2(n=3)→"done"→完成。断言宽松为"运行中或完成标记出现"。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui.py -k subagent tests/test_tui_app.py -k subagent -v`
Expected: FAIL（`_line_style` 未返回新样式 / 无标注行）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/__init__.py` `_line_style` 改为：

```python
def _line_style(line: str) -> str:
    if line.startswith("> user:"):
        return "bold cyan"
    if line.startswith("[subagent]"):
        return "green" if "✓" in line else "yellow"
    if line.startswith("[tool]"):
        parts = line[len("[tool]"):].split()
        if len(parts) > 1:
            if parts[0] == "use_skill":
                return "magenta"
            if parts[0] == "dispatch_subagent":
                return "bold cyan"
        return "dim red" if len(parts) > 1 and parts[1] == "failed" else "dim"
    if line.startswith("[agent] stopped"):
        return "yellow"
    if line.startswith("[session"):
        return "magenta"
    if line.startswith("assistant:"):
        return "default"
    return "dim"
```

`code_agent/code_agent/tui/app.py`：
1) `__init__` 加 `self._subagent_idx: int | None = None`。
2) `_on_tool_start`：
```python
    def _on_tool_start(self, name: str) -> None:
        if name != "dispatch_subagent":
            return
        log = self.query_one("#log", ConversationLog)
        log.append("[subagent] 子智能体运行中…")
        self._subagent_idx = len(log._lines) - 1
```
3) `_on_tool` 加分支：
```python
    def _on_tool(self, name, res) -> None:
        log = self.query_one("#log", ConversationLog)
        if name == "dispatch_subagent" and self._subagent_idx is not None:
            log.update_line(self._subagent_idx, "[subagent] ✓ 完成")
            self._subagent_idx = None
        log.append(format_tool(name, res.ok, res.truncated, res.output))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui.py -k subagent tests/test_tui_app.py -k subagent -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/__init__.py code_agent/code_agent/tui/app.py tests/test_tui.py tests/test_tui_app.py
git commit -m "feat: app.py 子智能体运行标注 + _line_style skill/subagent 样式（Task 4/8）"
```

---

### Task 5: app.py —— `!` 终端命令

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`（顶部 `import subprocess`、`_bang_busy`、`on_input_submitted` `!` 分支、`_run_bang` / `_run_bang_thread` / `_append_bang_result`）
- Modify: `code_agent/tests/test_tui_app.py`

**Interfaces:**
- Consumes: 无（subprocess + call_from_thread）。
- Produces:
  - `__init__` 加 `self._bang_busy = False`；`_busy()` 改为 `(self._worker alive) or self._bang_busy`。
  - `on_input_submitted`：在 `value.startswith("/")` 分支之后、`if self._busy():` 之前加 `!` 分支：
    ```python
        if value.startswith("!"):
            cmd = value[1:].strip()
            if not cmd:
                return
            if self._busy():
                self.notify("任务正在运行中", severity="warning")
                return
            self._run_bang(cmd)
            return
    ```
  - `_run_bang(cmd)`：`self._bang_busy = True`；`log.append(f"$ {cmd}")`；`_refresh_status("running")`；起 daemon 线程跑 `_run_bang_thread`。
  - `_run_bang_thread(cmd)`：`subprocess.run(cmd, shell=True, cwd=self.session.workdir, capture_output=True, text=True, timeout=120)`；成功 → `call_from_thread(_append_bang_result(out, returncode))`；`TimeoutExpired` → `call_from_thread(_append_bang_result("", None))`；其它异常 → `call_from_thread(_append_bang_error(str(e)))`。
  - `_append_bang_result(out, code)` / `_append_bang_error(msg)`：回显输出 + `[exit <code>]` / `[command failed: <msg>]`；`_bang_busy=False`；`_refresh_status("idle")`。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tui_app.py`：

```python
def test_app_bang_command(workdir, tmp_path, monkeypatch):
    from code_agent.tui import app as tui_app

    class _R:
        returncode = 0
        stdout = "hello world"
        stderr = ""

    monkeypatch.setattr(tui_app.subprocess, "run", lambda *a, **k: _R())
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "!echo hi"
            await pilot.press("enter")
            for _ in range(80):
                log = app.query_one("#log")
                if "hello world" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "$ echo hi" in text
            assert "hello world" in text
            assert "[exit 0]" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_bang_timeout(workdir, tmp_path, monkeypatch):
    import subprocess as sp
    from code_agent.tui import app as tui_app

    def _boom(*a, **k):
        raise sp.TimeoutExpired("cmd", 120)

    monkeypatch.setattr(tui_app.subprocess, "run", _boom)
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "!sleep 999"
            await pilot.press("enter")
            for _ in range(80):
                log = app.query_one("#log")
                if "timed out" in "".join(l.plain for l in log._lines):
                    break
                await asyncio.sleep(0.02)
            text = "".join(l.plain for l in app.query_one("#log")._lines)
            assert "[command timed out after 120s]" in text
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py -k bang -v`
Expected: FAIL（无 `!` 分支/`_run_bang`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/app.py`：
1) 顶部 `import subprocess`（与其它 import 一起）。
2) `__init__` 加 `self._bang_busy = False`。
3) `_busy()` 改为：
```python
    def _busy(self) -> bool:
        return (self._worker is not None and self._worker.is_alive()) or self._bang_busy
```
4) `on_input_submitted` 在 `/` 分支后加 `!` 分支（见 Interfaces）。
5) 追加方法：
```python
    def _run_bang(self, cmd: str) -> None:
        self._bang_busy = True
        self.query_one("#log", ConversationLog).append(f"$ {cmd}")
        self._refresh_status("running")
        threading.Thread(target=self._run_bang_thread, args=(cmd,), daemon=True).start()

    def _run_bang_thread(self, cmd: str) -> None:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=self.session.workdir,
                                  capture_output=True, text=True, timeout=120)
            out = (proc.stdout or "")
            if proc.stderr:
                out += "\n" + proc.stderr
            self.app.call_from_thread(lambda: self._append_bang_result(out, proc.returncode))
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(lambda: self._append_bang_result("", None))
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(lambda: self._append_bang_error(f"{type(e).__name__}: {e}"))

    def _append_bang_result(self, out: str, code) -> None:
        log = self.query_one("#log", ConversationLog)
        if out.strip():
            log.append(out.rstrip())
        log.append("[command timed out after 120s]" if code is None else f"[exit {code}]")
        self._bang_busy = False
        self._refresh_status("idle")

    def _append_bang_error(self, msg: str) -> None:
        self.query_one("#log", ConversationLog).append(f"[command failed: {msg}]")
        self._bang_busy = False
        self._refresh_status("idle")
```
（`threading` 需 import——`code_agent/tui/app.py` 顶部加 `import threading`。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py -k bang -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/app.py tests/test_tui_app.py
git commit -m "feat: app.py ! 终端命令（后台执行回显，Task 5/8）"
```

---

### Task 6: app.py —— Ctrl+L 重排 + 移除命令面板

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`
- Modify: `code_agent/tests/test_tui_app.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `action_toggle_sessions`：切换 `visible` + `refresh_from` 后，`#log` 强制重渲染 body 并钳制滚动偏移（`log._update_body()`；`log.scroll_to(y=min(log.scroll_offset.y, log.max_scroll_y))`；try/except 兜底）。
  - 命令面板移除：`COMMANDS = ()`；`BINDINGS` 追加 `Binding("ctrl+p", "noop", "disabled")`；`action_noop` 空实现。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_tui_app.py`：

```python
def test_app_commands_and_bindings():
    from code_agent.tui.app import CodeAgentApp
    assert CodeAgentApp.COMMANDS == ()
    keys = [b.key for b in CodeAgentApp.BINDINGS]
    assert "ctrl+p" in keys and "ctrl+q" in keys and "ctrl+n" in keys and "ctrl+l" in keys


def test_app_toggle_sessions_no_error(workdir, tmp_path):
    app = _make_app(workdir, tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+l")
            await pilot.press("ctrl+l")
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py -k "commands or toggle" -v`
Expected: FAIL（`COMMANDS` 未定义 / `ctrl+p` 不在绑定）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/app.py`：
1) 类属性加 `COMMANDS = ()`；`BINDINGS` 追加：
```python
        Binding("ctrl+p", "noop", "disabled"),
```
2) `action_toggle_sessions` 改为：
```python
    def action_toggle_sessions(self) -> None:
        sl = self.query_one("#sessions", SessionList)
        sl.toggle_class("visible")
        sl.refresh_from(self.store)
        log = self.query_one("#log", ConversationLog)
        try:
            log._update_body()
            log.scroll_to(y=min(log.scroll_offset.y, log.max_scroll_y))
        except Exception:
            pass  # 布局边缘兜底
```
3) 新增空动作：
```python
    def action_noop(self) -> None:
        pass
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py -k "commands or toggle" -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/app.py tests/test_tui_app.py
git commit -m "feat: app.py Ctrl+L 重排+钳制滚动 + 移除命令面板（Task 6/8）"
```

---

### Task 7: 文档同步 + ADR-021

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/README.md`
- Modify: `../.agent/03-decisions.md`（工作区根，不入库）

**Interfaces:**
- Consumes: Task 1-6 的实现。
- Produces: 文档权威源与实现一致；ADR-021。

- [ ] **Step 1: architecture.md**

- §3 agent.py `run_task` 签名补 `on_assistant_start` / `on_tool_start`。
- §3 tui：`!` 命令、子智能体运行标注、命令面板移除（`COMMANDS=()`）、布局切换钳制滚动。

- [ ] **Step 2: development.md**

- §2 `--interactive`：快捷键表去掉 Ctrl+P；补 `!cmd` 用法；子智能体运行标注说明。
- §3 测试目录：test_tui_app.py 说明补多轮/`!`/标注用例。

- [ ] **Step 3: design.md**

- §6 勾选：`- [x] TUI 打磨（多轮渲染修复、Ctrl+L 布局修复、子智能体运行标注、! 命令、移除命令面板，ADR-021）`。
- §8 追加：`18. [x] 迭代增强：TUI 打磨（ADR-021，设计见 docs/superpowers/specs/2026-08-30-tui-polish-design.md）`。
- 更新测试计数到实际值。

- [ ] **Step 4: README.md**

- TUI 快捷键表去掉 Ctrl+P、补 `!cmd`；计数实际值。

- [ ] **Step 5: .agent/03-decisions.md（工作区根）**

追加 ADR-021：

```markdown
## ADR-021：TUI 打磨（多轮渲染/布局重叠/子智能体状态/! 命令/移除命令面板）
- **日期**：2026-08-30
- **状态**：已实施
- **背景**：TUI 冒烟反馈：①工具行错位（多轮文本合并进第一行，刷新才对齐）；②Ctrl+L 滚动后布局重叠；③子智能体/skill 需运行状态感知；④需要 `!` 直接执行终端命令；⑤命令面板（主题/截图）冗余。
- **选项**：多轮渲染修 `_assistant_idx` 单行 / 每回合新建行；`!` 走 agent / 用户直跑 shell。
- **决策**：`run_task` 新增 `on_assistant_start`/`on_tool_start` 回调，每回合新建 assistant 行重钉索引（工具行自然插对位）；`action_toggle_sessions` 重渲染 body + 钳制滚动偏移；`on_tool_start` 标注"子智能体运行中…/✓ 完成"（不回显报告）；`!cmd` 后台线程跑 shell 回显（120s 超时、busy 互斥、不走 policy）；`COMMANDS=()` + `ctrl+p` noop 移除命令面板。
- **理由**：回调式事件驱动与现有桥一致、可测；`!` 是用户主动命令无需 policy；命令面板与"快捷功能极少"的定位不符。
- **影响**：`run_task` 签名 +2 回调（向后兼容）；TUI 交互补 `!`、子智能体标注；命令面板消失。
```

- [ ] **Step 6: 运行全量测试 + 提交**

Run: `uv run pytest tests/ -q`
Expected: 全绿（实际总数以运行结果为准，文档写实际值）
```bash
git add code_agent/docs/ README.md
git commit -m "docs: 同步 TUI 打磨文档并记录 ADR-021（架构/开发/设计/README）"
```

---

### Task 8: 全量回归 + 真实冒烟 + 凭据复核

**Files:**
- 无（验证与收尾）。

**Interfaces:**
- Consumes: Task 1-7 全部实现。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全绿

- [ ] **Step 2: CLI --help 正常**

Run: `uv run python -m code_agent --help`
Expected: 正常输出

- [ ] **Step 3: TUI 真实冒烟（用户终端）**

经用户确认后运行：
```bash
uv run python -m code_agent --interactive
```
Expected（用户反馈核验）：多轮任务（如"读 a.txt 再总结"）工具行顺序正确；滚动后 `Ctrl+L` 无重叠；`!pwd` 回显输出；`Ctrl+P` 无命令面板；子智能体任务显示"运行中…/✓ 完成"。

- [ ] **Step 4: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

- [ ] **Step 5: 收尾确认**

- 文档权威源与代码同步；计数实际值。
- `git status` 干净；提交历史完整（本迭代 8 个 commit + 可能修复波，未改写历史）。
