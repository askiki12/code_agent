# Textual TUI 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **API 提示**：Textual 版本间 API 有差异。每个 Task 的实现者必须在动手前用 `uv run python -c "import textual; print(textual.__version__)"` 确认版本，并以该版本实际 API 为准；若计划代码与安装版本不符，实现者可做最小适配并在报告中注明差异（如 `VerticalScroll`/`OptionList`/`run_test` 的类名与属性）。

**Goal:** 把 ADR-019 的 rich TUI 重构为 Textual 全屏 TUI：可滚动对话区（滚轮+键盘、自动到底可回看）、会话列表面板、快捷键（Ctrl+Q/N/L）、后台线程跑 agent 循环、色彩分层界面。

**Architecture:** `code_agent/tui.py` 单文件改为 `code_agent/tui/` 包（`__init__.py` 导出 `run_tui` + 纯函数；`widgets.py` 自定义部件；`worker.py` 后台线程 + `App.call_from_thread` 桥；`app.py` 为 `CodeAgentApp(App)`）。`run_tui` 签名不变，`cli.py` 零改动。

**Tech Stack:** Python 3.11+，`requests` + `rich>=13` + `textual>=0.80`，`pytest`。测试离线（假 llm/假 session + Textual `run_test`）。

## Global Constraints

- 禁止任何 agent 框架/SDK；Textual 是终端 UI 框架，非 agent 框架，允许（ADR-020，supersede ADR-019 渲染方案；rich 保留）。
- `run_tui(session, store, workspace=None, *, model="") -> None` 签名与行为保持；`cli.py` 接线零改动。
- 布局：Header + 可滚动 ConversationLog + SessionList（Ctrl+L 切换）+ 输入栏 + Footer（Ctrl+Q/N/L）。
- `run_task` 放后台线程；UI 事件经 `App.call_from_thread` 桥回主循环；权限 ask 经 `threading.Event` 阻塞唤醒。
- 复用：`cli.handle_command`、`format_user/format_assistant/format_tool`（迁移）、`session.ask` 注入、`Conversation` 代理项清洗（已修）。
- 色彩分层：`> user:` 青加粗 / `assistant:` 默认 / `[tool] ... ok` 灰 / `failed` 红 / `[agent] stopped` 黄 / `[session` 品红。
- 凭据纪律不变；测试全部离线；每个任务结束必须测试通过并提交（不 rebase/改写历史）。

---

### Task 1: 依赖 —— 新增 `textual>=0.80` + `uv sync`

**Files:**
- Modify: `code_agent/pyproject.toml`（dependencies 加 `textual`）
- Modify: `code_agent/uv.lock`（`uv sync` 自动更新）

**Interfaces:**
- Consumes: 无。
- Produces: `textual` 可在 uv 环境导入；记录实际版本（供后续 Task 对照 API）。

- [ ] **Step 1: 修改 pyproject.toml**

`code_agent/pyproject.toml` 的 `dependencies` 行：
```toml
dependencies = ["requests>=2.31", "rich>=13", "textual>=0.80"]
```

- [ ] **Step 2: uv sync 安装**

Run: `uv sync`
Expected: 成功，`uv.lock` 更新（含 textual）

- [ ] **Step 3: 验证可导入 + 记录版本 + 全量回归**

Run: `uv run python -c "import textual; print(textual.__version__)"`
Expected: 输出版本号（实现者把版本记入报告，供后续 Task 对照）
Run: `uv run pytest tests/ -q`
Expected: 全绿（239）

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "build: 新增 textual 运行时依赖（TUI 重构前置，Task 1/7）"
```

---

### Task 2: `tui/` 包骨架 —— 迁移纯函数 + 删除旧 tui.py

**Files:**
- Create: `code_agent/code_agent/tui/__init__.py`
- Create: `code_agent/code_agent/tui/widgets.py`（占位 docstring，Task 3 填充）
- Create: `code_agent/code_agent/tui/worker.py`（占位 docstring，Task 4 填充）
- Create: `code_agent/code_agent/tui/app.py`（占位 docstring，Task 5 填充）
- Delete: `code_agent/code_agent/tui.py`
- Modify: `code_agent/tests/test_tui.py`（迁移纯函数用例，导入路径不变）

**Interfaces:**
- Consumes: 现有 `format_user/format_assistant/format_tool/_append_tool_line`（从旧 tui.py 迁移）。
- Produces:
  - `code_agent.tui.run_tui(session, store, workspace=None, *, model="")`——Task 5 前为占位（`raise NotImplementedError` 或打印提示）；Task 5 改为委托 `CodeAgentApp`。
  - `format_user/format_assistant/format_tool/_append_tool_line` 原样迁移（纯函数，导出自 `__init__.py`）。
  - `_line_style(line: str) -> str`——按行前缀返回 rich 样式名：`"> user:"` → `"bold cyan"`；`"[tool]"` → `"dim"`；`"[agent] stopped"` → `"yellow"`；`"[session"` → `"magenta"`；`"assistant:"` → `"default"`；其它 → `"dim"`。
  - `_append_tool_line(conversation, name, res)` 保持（内部用 `format_tool` + `_line_style` 无关，仅 append 文本）。

- [ ] **Step 1: 写失败测试（`_line_style`）**

追加到 `code_agent/tests/test_tui.py`：

```python
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
```

注意：`test_line_style_tool_ok_and_failed` 期望 failed 行样式为 `"dim red"`（灰底红字），因此 `_line_style` 对 `"[tool]"` 且含 `" failed"` 的行返回 `"dim red"`，否则 `"dim"`。请按此实现（含 `"ok"` 与 `"failed"` 分支）。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui.py -v`
Expected: FAIL（`ImportError: cannot import name '_line_style' from 'code_agent.tui'`——旧 tui.py 是模块，`__init__.py` 尚无此名）

- [ ] **Step 3: 实现**

1) 创建目录与 `code_agent/code_agent/tui/__init__.py`：

```python
"""Textual terminal UI for interactive mode (replaces the rich tui.py)."""
from __future__ import annotations


def format_user(content: str) -> str:
    return f"> user: {content}"


def format_assistant(content: str) -> str:
    return f"assistant: {content}"


def format_tool(name: str, ok: bool, truncated: bool, output: str) -> str:
    status = "ok" if ok else "failed"
    if truncated:
        status += " (truncated)"
    first = output.splitlines()[0] if output.strip() else ""
    return f"[tool] {name} {status}" + (f" | {first}" if first else "")


def _append_tool_line(conversation: list[str], name: str, res) -> None:
    conversation.append(format_tool(name, res.ok, res.truncated, res.output))


def _line_style(line: str) -> str:
    if line.startswith("> user:"):
        return "bold cyan"
    if line.startswith("[tool]"):
        return "dim red" if " failed" in line else "dim"
    if line.startswith("[agent] stopped"):
        return "yellow"
    if line.startswith("[session"):
        return "magenta"
    if line.startswith("assistant:"):
        return "default"
    return "dim"


def run_tui(session, store, workspace=None, *, model: str = "") -> None:
    # Task 5 委托 CodeAgentApp；此处暂不允许实际进入（避免误导）。
    raise NotImplementedError("run_tui wired in Task 5")
```

2) 创建 `widgets.py` / `worker.py` / `app.py` 空文件（仅 docstring）：
`code_agent/code_agent/tui/widgets.py`：
```python
"""Custom widgets for the code_agent TUI (filled in Task 3)."""
```
`code_agent/code_agent/tui/worker.py`：
```python
"""Background worker bridging run_task to the Textual UI thread (filled in Task 4)."""
```
`code_agent/code_agent/tui/app.py`：
```python
"""CodeAgentApp: the Textual application (filled in Task 5)."""
```

3) 删除 `code_agent/code_agent/tui.py`：
Run: `git rm code_agent/code_agent/tui.py`

4) `tests/test_tui.py` 现有导入 `from code_agent.tui import format_*` 保持可用（`__init__.py` 导出同名函数）；旧 `test_run_tui_smoke`（StubConsole/StubLive，针对 rich 版）删除——Task 5 将加 Textual 版 App 冒烟。若 `test_tui.py` 里有 `run_tui` 导入，一并删除。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui.py -v`
Expected: PASS（format_* 迁移 + _line_style 用例）
Run: `uv run pytest tests/ -q`
Expected: 全绿（239 - 1(删旧冒烟) + 5(_line_style) = 243；以实际为准）

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/ code_agent/code_agent/tui.py tests/test_tui.py
git commit -m "refactor: tui.py → tui/ 包（迁移纯函数 + _line_style，Task 2/7）"
```

---

### Task 3: `widgets.py` —— StatusBar / ConversationLog / SessionList / PromptInput

**Files:**
- Modify: `code_agent/code_agent/tui/widgets.py`
- Create: `code_agent/tests/test_tui_widgets.py`

**Interfaces:**
- Consumes: Task 2 的 `_line_style`（`from code_agent.tui import _line_style`）。
- Produces（`code_agent/tui/widgets.py`）：
  - `class StatusBar(Widget)`：`update_status(state: str, model: str = "", session_id: str = "", workspace_line: str = "")`——渲染状态行（含 `● idle`/`● running` 徽章色）。
  - `class ConversationLog(VerticalScroll)`：`append(text: str)`（末行追加样式 `_line_style`）、`update_last(text: str)`（覆盖末行，供流式）、`update_line(idx: int, text: str)`（覆盖指定索引行，供流式期间工具行插入时保持 assistant 行定位）、`clear()`；内部维护 `_lines: list[Text]` 渲染到内嵌 `Static`；`append`/`update_last`/`update_line` 后若接近底部则 `scroll_end(animate=False)`（`_near_bottom()` 启发式），否则保持用户滚动位置。
  - `class SessionList(OptionList)`：`refresh_from(store)`（用 `store.list_sessions()` 填充，`Option(prompt, id=session_id)`）。
  - `class PromptInput(Input)`：`set_ask_mode(prompt: str)` / `clear_ask_mode()`——ask 时 placeholder/值变确认提示。

**Textual API 适配**：`VerticalScroll`/`OptionList`/`Input`/`Widget`/`Static` 从 `textual.widgets` / `textual` 导入；`_near_bottom` 用 `self.scroll_position.y` 与可滚动高度比较（以安装版本实际属性为准，差异在报告中注明）。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_tui_widgets.py`（不依赖真实终端，直接构造部件并检查内部状态；若 `run_test` 渲染部件复杂，用 `app.run_test()` 挂载一个最小 App 冒烟）：

```python
import pytest

from code_agent.tui.widgets import ConversationLog, SessionList


def test_conversation_log_append_and_update_last():
    log = ConversationLog()
    log.append("> user: hi")
    log.append("assistant: ")
    assert log._lines[0].plain == "> user: hi"
    log.update_last("assistant: hello")
    assert log._lines[-1].plain == "assistant: hello"
    assert len(log._lines) == 2


def test_conversation_log_clear():
    log = ConversationLog()
    log.append("x")
    log.clear()
    assert log._lines == []


def test_conversation_log_update_line():
    log = ConversationLog()
    log.append("assistant: ")
    log.append("[tool] read_file ok | a")
    log.update_line(0, "assistant: hello")
    assert log._lines[0].plain == "assistant: hello"
    assert log._lines[1].plain == "[tool] read_file ok | a"


def test_conversation_log_line_styles():
    log = ConversationLog()
    log.append("> user: hi")
    log.append("[tool] read_file ok | a")
    log.append("[agent] stopped: boom")
    styles = [line.style for line in log._lines]
    assert styles == ["bold cyan", "dim", "yellow"]


def test_session_list_refresh(tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    store.create("t1")
    sl = SessionList()
    sl.refresh_from(store)
    assert sl.option_count == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_widgets.py -v`
Expected: FAIL（`ImportError` widgets 未实现）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/widgets.py`：

```python
"""Custom widgets for the code_agent TUI."""
from __future__ import annotations

from code_agent.tui import _line_style
from textual.widgets import Input, OptionList, Static, VerticalScroll
from textual.widget import Widget
from textual.widgets.option_list import Option
from rich.text import Text


class StatusBar(Widget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = Text("idle", style="green")

    def update_status(self, state: str, model: str = "", session_id: str = "", workspace_line: str = "") -> None:
        color = "green" if state == "idle" else "yellow"
        parts = []
        if workspace_line:
            parts.append(workspace_line)
        if model:
            parts.append(f"model: {model}")
        if session_id:
            parts.append(f"session: {session_id}")
        head = " | ".join(parts)
        dot = "●"
        self._text = Text()
        self._text.append(head + ("  " if head else "") + dot + " ", style="default")
        self._text.append(state, style=color)
        self.refresh()

    def render(self) -> Text:
        return self._text


class ConversationLog(VerticalScroll):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lines: list[Text] = []

    def compose(self):
        yield Static("", id="body")

    def _render_body(self) -> Text:
        t = Text()
        for line in self._lines:
            t.append_text(line)
            t.append("\n")
        return t

    def _update_body(self) -> None:
        body = self.query_one("#body", Static)
        body.update(self._render_body())
        if self._near_bottom():
            self.scroll_end(animate=False)

    def _near_bottom(self) -> bool:
        try:
            return self.scroll_position.y >= self.max_scroll_y - 1
        except Exception:
            return True

    def append(self, text: str) -> None:
        self._lines.append(Text(text, style=_line_style(text)))
        self._update_body()

    def update_last(self, text: str) -> None:
        if self._lines:
            self._lines[-1] = Text(text, style=_line_style(text))
            self._update_body()
        else:
            self.append(text)

    def update_line(self, idx: int, text: str) -> None:
        if 0 <= idx < len(self._lines):
            self._lines[idx] = Text(text, style=_line_style(text))
            self._update_body()

    def clear(self) -> None:
        self._lines = []
        self._update_body()


class SessionList(OptionList):
    def refresh_from(self, store) -> None:
        self.clear_options()
        for s in store.list_sessions():
            title = s.get("title") or ""
            prompt = f"{s['id'][-12:]}  {title}"
            self.add_option(Option(prompt, id=s["id"]))


class PromptInput(Input):
    def set_ask_mode(self, prompt: str) -> None:
        self.value = ""
        self.placeholder = prompt

    def clear_ask_mode(self) -> None:
        self.placeholder = "❯ 输入任务（/ 开头为命令）"
```

> 说明：`max_scroll_y` 若在安装版本不存在，改用 `self.scrollable_content_size.height - self.size.height` 估算，并在报告中注明。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_widgets.py -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/widgets.py tests/test_tui_widgets.py
git commit -m "feat: tui/widgets.py 状态栏/可滚动对话/会话列表/输入（Task 3/7）"
```

---

### Task 4: `worker.py` —— AgentWorker（后台线程 + 异步桥）

**Files:**
- Modify: `code_agent/code_agent/tui/worker.py`
- Create: `code_agent/tests/test_tui_worker.py`

**Interfaces:**
- Consumes: 无（回调注入）。
- Produces:
  - `class AgentWorker`：
    - `__init__(self, app, session, *, on_delta, on_tool, on_done, on_ask=None)`——`app` 提供 `call_from_thread`；`on_delta(chunk)` / `on_tool(name, res)` / `on_done(result)` 在 UI 线程被调；`on_ask(prompt, responder)` 在 UI 线程被调，`responder(answer)` 从 UI 线程回填。
    - `start(task: str) -> None`：起 daemon 线程跑 `session.run_task(task, on_delta=..., on_tool=..., ask=self._ask)`。
    - `_delta(chunk)` / `_tool(name, res)`：`self.app.call_from_thread(...)`。
    - `_ask(prompt) -> str`：`threading.Event` 阻塞；UI 侧 `on_ask(prompt, responder)` 展示确认 → 用户输入 → `responder(answer)` 置 Event + 结果。
    - `is_alive() -> bool`。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_tui_worker.py`：

```python
import threading
import time

import pytest

from code_agent.llm import LLMResponse, ToolCall
from code_agent.tui.worker import AgentWorker


class _FakeLLM:
    def __init__(self):
        self.chunks = ["Hel", "lo"]
        self.calls = 0

    def chat(self, messages, tools=None, on_delta=None):
        self.calls += 1
        for c in self.chunks:
            if on_delta:
                on_delta(c)
        return LLMResponse(content="hello", tool_calls=[])


class _FakeApp:
    def __init__(self):
        self.ui_ops = []

    def call_from_thread(self, fn):
        fn()
        self.ui_ops.append(fn.__name__)


def _make_session(workdir, llm):
    from code_agent.agent import AgentSession
    return AgentSession(workdir=workdir, llm=llm, max_iterations=3)


def test_worker_streams_and_done(workdir):
    llm = _FakeLLM()
    session = _make_session(workdir, llm)
    app = _FakeApp()
    deltas, tools, done = [], [], []

    w = AgentWorker(
        app, session,
        on_delta=lambda c: deltas.append(c),
        on_tool=lambda n, r: tools.append((n, r)),
        on_done=lambda r: done.append(r),
    )
    w.start("hi")
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.02)
    assert done and done[0].final_text == "hello"
    assert "".join(deltas) == "Hel" + "Lo"
    assert app.ui_ops  # 桥被调用
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_worker.py -v`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/worker.py`：

```python
"""Background worker bridging run_task to the Textual UI thread."""
from __future__ import annotations

import threading


class AgentWorker:
    def __init__(self, app, session, *, on_delta, on_tool, on_done, on_ask=None) -> None:
        self.app = app
        self.session = session
        self._on_delta = on_delta
        self._on_tool = on_tool
        self._on_done = on_done
        self._on_ask = on_ask
        self._thread: threading.Thread | None = None

    def start(self, task: str) -> None:
        self._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, task: str) -> None:
        try:
            result = self.session.run_task(task, on_delta=self._delta, on_tool=self._tool, ask=self._ask)
        except Exception as e:  # noqa: BLE001
            from code_agent.agent import RunResult
            result = RunResult(final_text="", iterations=0, finished=False, reason=f"worker crash: {type(e).__name__}: {e}")
        self.app.call_from_thread(lambda: self._on_done(result))

    def _delta(self, chunk: str) -> None:
        self.app.call_from_thread(lambda: self._on_delta(chunk))

    def _tool(self, name, res) -> None:
        self.app.call_from_thread(lambda: self._on_tool(name, res))

    def _ask(self, prompt: str) -> str:
        ev = threading.Event()
        holder = {"answer": ""}

        def responder(answer: str) -> None:
            holder["answer"] = answer
            ev.set()

        self.app.call_from_thread(lambda: self._on_ask(prompt, responder))
        ev.wait(timeout=600)
        return holder["answer"]
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_worker.py -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/worker.py tests/test_tui_worker.py
git commit -m "feat: tui/worker.py 后台线程 + call_from_thread 桥（Task 4/7）"
```

---

### Task 5: `app.py` —— CodeAgentApp + run_tui 接线 + App 冒烟

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`
- Modify: `code_agent/code_agent/tui/__init__.py`（`run_tui` 委托 App）
- Modify: `code_agent/tests/test_tui_app.py`（新增）
- Modify: `code_agent/tests/test_tui.py`（若含旧 run_tui 引用则清理）

**Interfaces:**
- Consumes: Task 3 widgets、Task 4 AgentWorker、Task 2 纯函数、`cli.handle_command`。
- Produces:
  - `CodeAgentApp(App)`：`BINDINGS = [Binding("ctrl+q","quit","Quit"), Binding("ctrl+n","new_session","New"), Binding("ctrl+l","toggle_sessions","Sessions")]`；`compose()` 返回 `Header` + `Horizontal(Vertical(ConversationLog, PromptInput), SessionList)` + `Footer`。
  - 事件：`on_input_submitted`（普通任务→起 worker；`/`→`handle_command` 输出进对话区；ask 模式→`responder`；运行中提交→提示）；`on_option_list_option_selected`（会话切换：`session.load_session` + 刷新对话区与状态栏）；`action_quit`/`action_new_session`/`action_toggle_sessions`。
  - worker 回调：`on_delta`→`ConversationLog.update_last`（流式）；`on_tool`→`append(format_tool)`；`on_done`→结束行 + 状态栏 idle + 会话 id 行。
  - ask：`on_ask(prompt, responder)`→`PromptInput.set_ask_mode` + 记住 responder；提交时 `responder(value)` + `clear_ask_mode`。
  - `run_tui`：`CodeAgentApp(session, store, workspace, model).run()`。
- `run_tui` 签名不变（`__init__.py` 替换 Task 2 占位）。

- [ ] **Step 1: 写失败测试**

`code_agent/tests/test_tui_app.py`（Textual `run_test`；注入假 llm 的 AgentSession + 真 store）：

```python
import asyncio

import pytest

from code_agent.agent import AgentSession
from code_agent.llm import LLMResponse
from code_agent.session import SessionStore
from code_agent.tui.app import CodeAgentApp


class _FakeLLM:
    def chat(self, messages, tools=None, on_delta=None):
        if on_delta:
            on_delta("答复内容")
        return LLMResponse(content="答复内容", tool_calls=[])


def _make_app(workdir, tmp_path):
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3)
    store = SessionStore(str(tmp_path / "sessions"))
    return CodeAgentApp(session, store, None, model="test")


async def test_app_enter_task_renders_user_and_assistant(workdir, tmp_path):
    app = _make_app(workdir, tmp_path)
    async with app.run_test() as pilot:
        inp = app.query_one("#input")
        inp.value = "hello"
        await pilot.press("enter")
        for _ in range(50):  # 轮询等待 worker 完成
            log = app.query_one("#log")
            text = "".join(l.plain for l in log._lines)
            if "答复内容" in text:
                break
            await asyncio.sleep(0.02)
        text = "".join(l.plain for l in log._lines)
        assert "> user: hello" in text
        assert "答复内容" in text
        await pilot.press("ctrl+q")


async def test_app_new_session_clears_log(workdir, tmp_path):
    app = _make_app(workdir, tmp_path)
    async with app.run_test() as pilot:
        inp = app.query_one("#input")
        inp.value = "hi"
        await pilot.press("enter")
        for _ in range(50):
            log = app.query_one("#log")
            if "答复内容" in log._lines[-1].plain:
                break
            await asyncio.sleep(0.02)
        await pilot.press("ctrl+n")
        log = app.query_one("#log")
        assert log._lines == []
        await pilot.press("ctrl+q")
```

> 若 `run_test` 下 worker 线程/轮询时序不稳，实现者可放宽轮询次数或在报告注明；`ctrl+q` 触发 Textual 内置 quit。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py -v`
Expected: FAIL（`ImportError` app 未实现）

- [ ] **Step 3: 最小实现**

`code_agent/code_agent/tui/app.py`：

```python
"""CodeAgentApp: the Textual application for code_agent."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from code_agent.cli import handle_command
from code_agent.tui import format_assistant, format_tool, format_user
from code_agent.tui.widgets import ConversationLog, PromptInput, SessionList, StatusBar
from code_agent.tui.worker import AgentWorker


class CodeAgentApp(App):
    CSS = """
    #log { height: 1fr; border: round $accent; }
    #input { dock: bottom; height: 3; }
    #status { height: 1; }
    #sessions { width: 34; border: round $primary; display: none; }
    #sessions.visible { display: block; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New"),
        Binding("ctrl+l", "toggle_sessions", "Sessions"),
    ]

    def __init__(self, session, store, workspace=None, *, model: str = "") -> None:
        super().__init__()
        self.session = session
        self.store = store
        self.workspace = workspace
        self.model = model
        self._worker: AgentWorker | None = None
        self._ask_responder = None
        self._assistant_idx = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield StatusBar(id="status")
        with Horizontal():
            yield ConversationLog(id="log")
            yield SessionList(id="sessions")
        yield PromptInput(placeholder="❯ 输入任务（/ 开头为命令）", id="input")
        yield Footer()

    def _workspace_line(self) -> str:
        return self.workspace.display() if self.workspace is not None else ""

    def _refresh_status(self, state: str) -> None:
        self.query_one("#status", StatusBar).update_status(
            state, model=self.model,
            session_id=self.session.session_id or "new",
            workspace_line=self._workspace_line(),
        )

    def on_mount(self) -> None:
        self._refresh_status("idle")
        self.query_one("#sessions", SessionList).refresh_from(self.store)
        # 恢复会话时按历史填充对话区
        self._reload_conversation()

    def _reload_conversation(self) -> None:
        log = self.query_one("#log", ConversationLog)
        log.clear()
        for m in self.session.conversation.messages:
            role = m.get("role")
            content = str(m.get("content", ""))
            if role == "user":
                log.append(format_user(content))
            elif role == "assistant":
                log.append(format_assistant(content))
            elif role == "tool":
                log.append(f"[tool] {m.get('name', '')} | {content.splitlines()[0] if content else ''}")

    def on_input_submitted(self, event) -> None:
        value = event.value.strip()
        self.query_one("#input", PromptInput).clear()
        event.input.clear()
        if not value:
            return
        if self._ask_responder is not None:
            responder, self._ask_responder = self._ask_responder, None
            self.query_one("#input", PromptInput).clear_ask_mode()
            responder(value)
            return
        if value.startswith("/"):
            keep, out = handle_command(value, self.session, self.store)
            log = self.query_one("#log", ConversationLog)
            for line in out:
                log.append(line)
            if not keep:
                self.exit()
                return
            if value == "/new":
                log.clear()
            self._refresh_status("idle")
            return
        if self._worker is not None and self._worker.is_alive():
            self.notify("agent 正在运行中", severity="warning")
            return
        self._start_task(value)

    def _start_task(self, task: str) -> None:
        log = self.query_one("#log", ConversationLog)
        log.append(format_user(task))
        log.append("assistant: ")
        self._assistant_idx = len(log._lines) - 1
        self._refresh_status("running")
        self._worker = AgentWorker(
            self, self.session,
            on_delta=lambda c: self._on_delta(c),
            on_tool=lambda n, r: self._on_tool(n, r),
            on_done=lambda r: self._on_done(r),
            on_ask=lambda p, resp: self._on_ask(p, resp),
        )
        self._worker.start(task)

    def _on_delta(self, chunk: str) -> None:
        log = self.query_one("#log", ConversationLog)
        current = log._lines[self._assistant_idx].plain + chunk
        log.update_line(self._assistant_idx, current)

    def _on_tool(self, name, res) -> None:
        self.query_one("#log", ConversationLog).append(format_tool(name, res.ok, res.truncated, res.output))

    def _on_done(self, result) -> None:
        log = self.query_one("#log", ConversationLog)
        if result.final_text:
            log.update_line(self._assistant_idx, format_assistant(result.final_text))
        if not result.finished:
            log.append(f"[agent] stopped: {result.reason}")
        if self.session.session_id:
            log.append(f"[session {self.session.session_id}]")
        self._refresh_status("idle")

    def _on_ask(self, prompt: str, responder) -> None:
        self._ask_responder = responder
        self.query_one("#input", PromptInput).set_ask_mode(prompt)
        self.query_one("#input", PromptInput).focus()

    def action_new_session(self) -> None:
        self.session.new_session()
        self.query_one("#log", ConversationLog).clear()
        self._refresh_status("idle")

    def action_toggle_sessions(self) -> None:
        sl = self.query_one("#sessions", SessionList)
        sl.toggle_class("visible")
        sl.refresh_from(self.store)

    def on_option_list_option_selected(self, event) -> None:
        sid = event.option.id
        if not sid:
            return
        try:
            self.session.load_session(sid)
        except KeyError:
            self.query_one("#log", ConversationLog).append(f"session not found: {sid}")
            return
        self._reload_conversation()
        self._refresh_status("idle")
```

`code_agent/code_agent/tui/__init__.py` 的 `run_tui` 替换为：

```python
def run_tui(session, store, workspace=None, *, model: str = "") -> None:
    from code_agent.tui.app import CodeAgentApp
    CodeAgentApp(session, store, workspace, model=model).run()
```

> Textual 具体 API（`run_test`/`Option.option.id`/CSS 类切换）以安装版本为准；`_on_delta` 直接改 `log._lines` 属内部约定（Task 3 定义），实现者保持该内部契约。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py -v`
Expected: PASS
Run: `uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add code_agent/code_agent/tui/app.py code_agent/code_agent/tui/__init__.py tests/test_tui_app.py
git commit -m "feat: tui/app.py CodeAgentApp + run_tui 接线（Task 5/7）"
```

---

### Task 6: 文档同步 + ADR-020

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/README.md`
- Modify: `../.agent/03-decisions.md`（工作区根，不入库）
- Modify: `../.agent/01-goals-and-boundaries.md` / `../.agent/04-engineering-standards.md`（依赖/测试范围同步，不入库）

**Interfaces:**
- Consumes: Task 1-5 的实现。
- Produces: 文档权威源与实现一致；ADR-020。

- [ ] **Step 1: architecture.md**

- 模块总览：`tui.py` 行改为 `code_agent/tui/ 包`（app/widgets/worker）；§3 接口（run_tui / CodeAgentApp / AgentWorker / ConversationLog 等）。

- [ ] **Step 2: development.md**

- §1 环境：运行时依赖 `requests` + `rich` + `textual`。
- §2 运行：`--interactive` TUI 说明更新——全屏 Textual：可滚动对话（滚轮/PageUp/Down）、会话列表面板（Ctrl+L）、快捷键 Ctrl+Q/N/L、流式与工具行。
- §3 测试目录：`test_tui.py` / `test_tui_widgets.py` / `test_tui_worker.py` / `test_tui_app.py`。

- [ ] **Step 3: design.md**

- §6 功能范围勾选：`- [x] Textual TUI 重构（可滚动对话+会话列表+快捷键，ADR-020）`。
- §8 开发路线追加：`17. [x] 迭代增强：Textual TUI 重构（ADR-020，设计见 docs/superpowers/specs/2026-08-30-tui-textual-design.md）`。
- 更新测试计数到实际值。

- [ ] **Step 4: README.md**

- 环境依赖：+`textual>=0.80`。
- 使用：TUI 快捷键表；结构 `tui/` 包。
- 测试计数更新到实际值。

- [ ] **Step 5: .agent/03-decisions.md（工作区根）**

追加 ADR-020（格式沿用前序）：

```markdown
## ADR-020：Textual TUI 重构（supersede ADR-019 渲染方案）
- **日期**：2026-08-30
- **状态**：已实施
- **背景**：rich TUI 冒烟暴露：对话区有界不可滚动（只能看最近几条）、界面朴素。用户要求用 Textual 重构，功能完整且美观。
- **选项**：rich Panel 自建滚动（脆弱）/ Textual 框架（成熟滚动/组件/快捷键）/ 保持现状。
- **决策**：新增运行时依赖 `textual>=0.80`（rich 保留作底层）；`tui.py` 重构为 `tui/` 包（app/widgets/worker）；`run_task` 后台线程 + `App.call_from_thread` 桥（UI 始终响应）；功能：可滚动对话（滚轮/PageUp/Down、自动到底可回看）+ 会话列表面板（Ctrl+L）+ 快捷键 Ctrl+Q/N/L + 色彩分层；`run_tui` 签名不变，cli 零改动。
- **理由**：Textual 原生滚动/组件/绑定，可交付美观且功能完整的 TUI；异步框架与阻塞 agent 循环用线程桥解耦；复用 handle_command/format_*/ask 注入，核心逻辑不变。
- **影响**：运行时依赖 +1（textual）；`code_agent/tui.py` → `code_agent/tui/` 包；旧 rich Live 渲染废弃（ADR-019 渲染方案被 supersede）。
```

- [ ] **Step 6: .agent/01 / .agent/04 同步（工作区根，不入库）**

- `.agent/01-goals-and-boundaries.md` §4 CLI 行更新为"`--interactive`（TTY 下 Textual 全屏 TUI，可回退纯文本）"。
- `.agent/04-engineering-standards.md` §2 依赖最小化补 `rich`/`textual`（TUI，ADR-019/020）；§4 必测范围加 `test_tui*.py`。

- [ ] **Step 7: 运行全量测试 + 提交**

Run: `uv run pytest tests/ -q`
Expected: 全绿（实际总数以运行结果为准，文档写实际值）
```bash
git add code_agent/docs/ README.md
git commit -m "docs: 同步 Textual TUI 文档并记录 ADR-020（架构/开发/设计/README）"
```

---

### Task 7: 全量回归 + 真实冒烟 + 凭据复核

**Files:**
- 无（验证与收尾）。

**Interfaces:**
- Consumes: Task 1-6 全部实现。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全绿

- [ ] **Step 2: CLI --help 正常**

Run: `uv run python -m code_agent --help`
Expected: 正常输出

- [ ] **Step 3: TUI 真实冒烟**

经用户确认后，由用户在实际终端运行：
```bash
uv run python -m code_agent --interactive
```
Expected（用户反馈核验）：全屏 TUI；提交任务后流式输出 + `[tool]` 行；滚轮/PageUp/Down 回看历史；Ctrl+L 会话列表切换；Ctrl+N 新建；Ctrl+Q 退出恢复终端。

- [ ] **Step 4: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

- [ ] **Step 5: 收尾确认**

- 文档权威源与代码同步（textual 依赖、TUI 说明、计数实际值）。
- `git status` 干净；提交历史完整（本迭代 7 个 commit + 可能修复波，未改写历史）。
