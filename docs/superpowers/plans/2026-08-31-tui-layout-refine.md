# TUI 布局调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 顶栏显示完整工作区路径与会话名，ctx/cache 移到底栏（与快捷键同行最右侧、紧凑格式），并修复切会话时 ctx/cache 不刷新的 bug。

**Architecture:** `workspace.py` 加 `path`、`session.py` 加 `get_title`、`agent.py` 加 `current_title` 并在 `load_session`/`new_session` 维护 `last_usage`；`tui/widgets.py` 用 `_fmt_ctx`/`_footer_stats` 取代 `_fmt_k`/`_usage_segments`、`StatusBar` 改收 session 标题、新增 `StatusFooter(Footer)`（右停靠 `#footer-stats`）；`tui/app.py` 用 `StatusFooter` 替换 `Footer()`、`_refresh_status` 同步刷底栏。

**Tech Stack:** Python 3.11+，Textual（tui），pytest（离线测试）。

## Global Constraints

- 所有命令在 `code_agent/` 目录内经 `uv run` 执行。
- 测试全离线；凭据不入库；提交前 `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"` 无命中。
- 底栏 stats 格式固定：`213.0k(21%)` 恒显一位小数（`_fmt_ctx(n) = f"{n/1000:.1f}k"`）；`pct = int(prompt/W*100)`（W 为上下文窗口）；cache 段 `cache:N%` 仅 `cached_tokens>0` 时显示；启发式前缀 `~`。
- 切会话语义：`load_session` 设启发式 `last_usage`；`new_session` 清空 `last_usage`。
- 无无关重构；本计划只改本 spec（docs/superpowers/specs/2026-08-31-tui-layout-refine-design.md）覆盖的文件。

---

### Task 1: Workspace.path + SessionStore.get_title

**Files:**
- Modify: `code_agent/code_agent/workspace.py`（加 `path` property，在 `name` property 后）
- Modify: `code_agent/code_agent/session.py`（加 `get_title`，在 `rename` 前）
- Test: `code_agent/tests/test_workspace.py`
- Test: `code_agent/tests/test_session.py`

**Interfaces:**
- Consumes: 既有 `Workspace._data` / `SessionStore._path` / `_read_meta`。
- Produces:
  - `Workspace.path -> str`（返回 `self._data["path"]`）。
  - `SessionStore.get_title(session_id: str) -> str`（文件缺失/坏 meta → ""）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace.py`：

```python
def test_workspace_path_is_realpath(tmp_path):
    import os
    ws = Workspace(str(tmp_path))
    assert ws.path == os.path.realpath(str(tmp_path))
```

追加到 `tests/test_session.py`：

```python
def test_get_title(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("hello title")
    assert store.get_title(sid) == "hello title"


def test_get_title_after_rename(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    store.rename(sid, "renamed")
    assert store.get_title(sid) == "renamed"


def test_get_title_missing(tmp_path):
    store = SessionStore(str(tmp_path))
    assert store.get_title("code_agent-nope") == ""


def test_get_title_corrupt_meta(tmp_path):
    import os
    store = SessionStore(str(tmp_path))
    sid = "code_agent-bad"
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write("garbage\n")
    assert store.get_title(sid) == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_workspace.py tests/test_session.py -v`
Expected: 新用例报 `AttributeError: 'Workspace' object has no attribute 'path'` / `'SessionStore' object has no attribute 'get_title'`。

- [ ] **Step 3: 实现**

`workspace.py` 在 `name` property 之后追加：

```python
    @property
    def path(self) -> str:
        return self._data["path"]
```

`session.py` 在 `rename` 方法之前追加：

```python
    def get_title(self, session_id: str) -> str:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            return ""
        meta = self._read_meta(path)
        if meta is None:
            return ""
        return meta.get("title") or ""
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_workspace.py tests/test_session.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/workspace.py code_agent/session.py tests/test_workspace.py tests/test_session.py
git commit -m "feat: Workspace.path + SessionStore.get_title（TUI 布局调整 Task 1/6）"
```

---

### Task 2: AgentSession current_title + load/new 的 last_usage

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Test: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `SessionStore.get_title`（Task 1）、`Usage`/`estimate_tokens`（已 import）。
- Produces:
  - `AgentSession.current_title() -> str`。
  - `load_session` 设置 `self.last_usage = Usage(..., heuristic=True)`（按已加载对话估算）。
  - `new_session` 设置 `self.last_usage = None`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_agent.py`）

```python
def test_load_session_sets_heuristic_last_usage(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("t")
    store.save(sid, [{"role": "user", "content": "hello world hello world"}])
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.last_usage is None
    s.load_session(sid)
    assert s.last_usage is not None
    assert s.last_usage.heuristic is True
    assert s.last_usage.prompt_tokens > 0


def test_new_session_clears_last_usage(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=_LLM(), store=store)
    s.run_task("task")
    assert s.last_usage is not None
    s.new_session()
    assert s.last_usage is None


def test_current_title(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("my title")
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store, session_id=sid)
    assert s.current_title() == "my title"


def test_current_title_new_session(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.current_title() == ""


def test_current_title_without_store(tmp_path):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=str(tmp_path), llm=object())
    assert s.current_title() == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -v`
Expected: `current_title` 不存在、`load_session` 不设 `last_usage`、`new_session` 不清空。

- [ ] **Step 3: 实现**

`agent.py` 的 `new_session` 改为：

```python
    def new_session(self) -> None:
        self.conversation = Conversation()
        self.conversation.add_system(self._system_prompt)
        self.session_id = None
        self.last_usage = None
```

`agent.py` 的 `load_session` 改为（追加 last_usage 估算）：

```python
    def load_session(self, session_id: str) -> None:
        if self.store is None:
            raise ValueError("no session store configured")
        _, messages = self.store.load(session_id)
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages)
        self.conversation = Conversation.from_jsonl(text, system_prompt=self._system_prompt)
        self.session_id = session_id
        self.last_usage = Usage(
            prompt_tokens=sum(
                estimate_tokens(str(m.get("content", ""))) for m in self.conversation.messages
            ),
            heuristic=True,
        )
```

`agent.py` 在 `_title` 方法之后追加：

```python
    def current_title(self) -> str:
        if self.session_id is None or self.store is None:
            return ""
        return self.store.get_title(self.session_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: AgentSession.current_title + load/new last_usage 语义（TUI 布局调整 Task 2/6）"
```

---

### Task 3: widgets.py 新 stats 函数 + StatusBar 改签名 + StatusFooter

**Files:**
- Modify: `code_agent/code_agent/tui/widgets.py`
- Test: `code_agent/tests/test_tui_widgets.py`

**Interfaces:**
- Consumes: `Usage`（duck typing）、`Footer`/`Static`（textual.widgets）。
- Produces:
  - `_fmt_ctx(n: int) -> str`（`f"{n/1000:.1f}k"`）。
  - `_footer_stats(usage, context_window) -> str`（None → ""；`213.0k(21%)`；cache 段仅 `cached_tokens>0`；启发式 `~`）。
  - `StatusBar.update_status(state, model="", session_title="", workspace_line="")`（去掉 usage/context_window 与宽度裁剪）。
  - `StatusFooter(Footer)`：右停靠 `#footer-stats` Static；`update_stats(text)`。
  - 删除 `_fmt_k` / `_usage_segments` / `_status_width` 与 `shutil` import。

- [ ] **Step 1: 改写测试**（把 `tests/test_tui_widgets.py` 第 109-169 行整段替换为）

```python
from code_agent.llm import Usage
from code_agent.tui.widgets import StatusBar, StatusFooter, _fmt_ctx, _footer_stats


def test_fmt_ctx():
    assert _fmt_ctx(0) == "0.0k"
    assert _fmt_ctx(90000) == "90.0k"
    assert _fmt_ctx(213000) == "213.0k"
    assert _fmt_ctx(12340) == "12.3k"


def test_footer_stats_full():
    assert _footer_stats(Usage(prompt_tokens=213000, cached_tokens=90000), 1_000_000) == "213.0k(21%) cache:42%"


def test_footer_stats_no_cache():
    assert _footer_stats(Usage(prompt_tokens=213000), 1_000_000) == "213.0k(21%)"


def test_footer_stats_none():
    assert _footer_stats(None, 1_000_000) == ""


def test_footer_stats_heuristic():
    assert _footer_stats(Usage(prompt_tokens=12340, heuristic=True), 1_000_000) == "~12.3k(1%)"


def test_status_bar_workspace_path_and_session_title():
    sb = StatusBar()
    sb.update_status("idle", model="m", session_title="我的会话", workspace_line="Workspace: /home/kiki/proj")
    plain = sb.render().plain
    assert "Workspace: /home/kiki/proj" in plain
    assert "session: 我的会话" in plain


def test_status_bar_new_session_title():
    sb = StatusBar()
    sb.update_status("idle", session_title="")
    assert "session: new" in sb.render().plain


def test_status_bar_no_ctx_segment():
    sb = StatusBar()
    sb.update_status("idle", model="m", session_title="s")
    plain = sb.render().plain
    assert "ctx" not in plain and "cache" not in plain and "%" not in plain


class _FooterApp(App):
    def compose(self):
        yield StatusFooter(id="footer")


def _run_footer(scenario) -> None:
    async def run():
        app = _FooterApp()
        async with app.run_test():
            footer = app.query_one("#footer", StatusFooter)
            await scenario(footer)

    asyncio.run(run())


def test_status_footer_update_stats():
    async def scenario(footer):
        footer.update_stats("213.0k(21%) cache:40%")
        await asyncio.sleep(0.01)
        assert footer.query_one("#footer-stats").render().plain == "213.0k(21%) cache:40%"

    _run_footer(scenario)


def test_status_footer_empty_stats():
    async def scenario(footer):
        footer.update_stats("")
        await asyncio.sleep(0.01)
        assert footer.query_one("#footer-stats").render().plain == ""

    _run_footer(scenario)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_widgets.py -v`
Expected: `_fmt_ctx`/`_footer_stats`/`StatusFooter` 不存在、StatusBar 新参数不被接受、旧 `_fmt_k`/`_usage_segments` 测试消失。

- [ ] **Step 3: 实现**

`widgets.py` 顶部：删除 `import shutil`；`from textual.widgets import Footer, Input, OptionList, Static`。

把 `_fmt_k` / `_usage_segments` / `_status_width` 三个函数整体替换为：

```python
def _fmt_ctx(n: int) -> str:
    return f"{n / 1000:.1f}k"


def _footer_stats(usage, context_window) -> str:
    if usage is None:
        return ""
    prompt = usage.prompt_tokens
    denom = context_window or prompt
    pct = int(prompt / denom * 100) if denom else 0
    prefix = "~" if usage.heuristic else ""
    parts = [f"{prefix}{_fmt_ctx(prompt)}({pct}%)"]
    if not usage.heuristic and prompt and usage.cached_tokens:
        parts.append(f"cache:{int(usage.cached_tokens / prompt * 100)}%")
    return " ".join(parts)
```

`StatusBar.update_status` 替换为：

```python
    def update_status(self, state: str, model: str = "", session_title: str = "",
                      workspace_line: str = "") -> None:
        color = "green" if state == "idle" else "yellow"
        parts = []
        if workspace_line:
            parts.append(workspace_line)
        if model:
            parts.append(f"model: {model}")
        parts.append(f"session: {session_title or 'new'}")
        head = " | ".join(parts)
        dot = "●"
        self._text = Text()
        self._text.append(head + ("  " if head else "") + dot + " ", style="default")
        self._text.append(state, style=color)
        self.refresh()
```

在 `StatusBar` 类之后追加 `StatusFooter`：

```python
class StatusFooter(Footer):
    DEFAULT_CSS = "#footer-stats { dock: right; margin-right: 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stats = Static("", id="footer-stats")

    def compose(self):
        yield self._stats
        yield from super().compose()

    def update_stats(self, text: str) -> None:
        self._stats.update(text)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_widgets.py tests/test_tui.py -v`
Expected: 全部 PASS（test_tui.py 是回归）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/tui/widgets.py tests/test_tui_widgets.py
git commit -m "feat: 底栏 StatusFooter stats + 顶栏 StatusBar 路径/会话名（TUI 布局调整 Task 3/6）"
```

---

### Task 4: app.py 接线（StatusFooter 替换 Footer + 刷新逻辑）

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`
- Test: `code_agent/tests/test_tui_app.py`

**Interfaces:**
- Consumes: `StatusFooter`/`_footer_stats`（Task 3）、`session.current_title()`/`last_usage`/`context_window`（Task 2）、`workspace.path`（Task 1）。
- Produces:
  - `compose()` 用 `StatusFooter(id="footer")` 替换 `Footer()`；`from textual.widgets import Header, Static`（去掉 Footer）。
  - `_workspace_line()` 返回 `Workspace: <path>`。
  - `_refresh_status(state)` 传 `session_title`，并同步 `footer.update_stats(_footer_stats(...))`。

- [ ] **Step 1: 改写/追加测试**（`tests/test_tui_app.py`）

把现有 `test_app_on_stats_updates_status` 替换为：

```python
def test_app_on_stats_updates_footer(workdir, tmp_path):
    from code_agent.llm import Usage
    from code_agent.tui.widgets import StatusFooter

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=12000, cached_tokens=3000))

    session = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=3, context_window=90000)
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hi"
            await pilot.press("enter")
            footer = None
            for _ in range(80):
                footer = app.query_one("#footer", StatusFooter)
                if "12.0k" in footer.query_one("#footer-stats").render().plain:
                    break
                await asyncio.sleep(0.02)
            txt = footer.query_one("#footer-stats").render().plain
            assert "12.0k(13%)" in txt
            assert "cache:25%" in txt
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

追加：

```python
def test_app_switch_session_updates_footer(workdir, tmp_path):
    from types import SimpleNamespace
    from code_agent.tui.widgets import StatusFooter
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("t")
    store.save(sid, [{"role": "user", "content": "hello world hello world"}])
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            app.on_option_list_option_selected(
                SimpleNamespace(option=SimpleNamespace(id=sid))
            )
            await asyncio.sleep(0.02)
            footer = app.query_one("#footer", StatusFooter)
            txt = footer.query_one("#footer-stats").render().plain
            assert txt.startswith("~")
            assert "(" in txt and ")" in txt
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_new_session_clears_footer_stats(workdir, tmp_path):
    from code_agent.tui.widgets import StatusFooter
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3)
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+n")
            footer = app.query_one("#footer", StatusFooter)
            assert footer.query_one("#footer-stats").render().plain == ""
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_top_bar_shows_session_title_and_path(workdir, tmp_path):
    from code_agent.workspace import Workspace
    from code_agent.tui.widgets import StatusBar
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    workspace = Workspace(workdir)
    app = CodeAgentApp(session, store, workspace, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            sb = app.query_one("#status", StatusBar)
            plain = sb.render().plain
            assert "Workspace: " in plain
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py -v`
Expected: `#footer` 不存在（旧测试断言 `#status` 里含 `ctx` 失败）；新测试找不到 `StatusFooter`。

- [ ] **Step 3: 实现**

`app.py`：
1. import 行 `from textual.widgets import Footer, Header, Static` → `from textual.widgets import Header, Static`。
2. widgets import 行追加：

```python
from code_agent.tui.widgets import (
    ConversationLog,
    PromptInput,
    SessionList,
    SkillList,
    StatusBar,
    StatusFooter,
    _footer_stats,
)
```

3. `compose()` 中 `yield Footer()` → `yield StatusFooter(id="footer")`。
4. `_workspace_line` 替换为：

```python
    def _workspace_line(self) -> str:
        if self.workspace is None:
            return ""
        return f"Workspace: {self.workspace.path}"
```

5. `_refresh_status` 替换为：

```python
    def _refresh_status(self, state: str) -> None:
        self.query_one("#status", StatusBar).update_status(
            state, model=self.model,
            session_title=self.session.current_title(),
            workspace_line=self._workspace_line(),
        )
        self.query_one("#footer", StatusFooter).update_stats(
            _footer_stats(
                getattr(self.session, "last_usage", None),
                getattr(self.session, "context_window", 0),
            )
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py tests/test_tui_worker.py tests/test_tui.py tests/test_tui_widgets.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/tui/app.py tests/test_tui_app.py
git commit -m "feat: TUI 底栏接线 + 顶栏路径/会话名 + 切会话刷新（TUI 布局调整 Task 4/6）"
```

---

### Task 5: 文档同步 + ADR-024

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/superpowers/specs/2026-08-31-observability-rename-design.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（ADR-024，不入库）

**Interfaces:**
- Consumes: Task 1-4 实际接口。

- [ ] **Step 1: 更新 `code_agent/docs/architecture.md`**

- workspace.py：加 `path` property。
- session.py：加 `get_title`。
- agent.py：加 `current_title`；`load_session` 设启发式 `last_usage`、`new_session` 清空。
- tui widgets：StatusBar 签名（state/model/session_title/workspace_line）、`StatusFooter`（右停靠 `#footer-stats` + `update_stats`）、`_fmt_ctx`/`_footer_stats`。
- tui app：compose 用 `StatusFooter(id="footer")`；`_workspace_line` 全路径；`_refresh_status` 同步底栏。

- [ ] **Step 2: 更新 `code_agent/docs/development.md`**

- §2 TUI 行为段：顶栏显示 `Workspace: <完整路径> | model | session: <会话名>`；底栏最右侧 stats `213.0k(21%) cache:40%`（启发式 `~`，无缓存隐藏）；切会话后 ctx 立即刷新为该会话估算值。
- §3 测试目录说明：test_tui_widgets 更新为 `_fmt_ctx`/`_footer_stats`/StatusBar/StatusFooter。

- [ ] **Step 3: 更新 `code_agent/docs/design.md`**

§6 功能范围勾选追加：

```
- [x] TUI 布局调整：顶栏完整路径 + 会话名；ctx/cache 移底栏紧凑格式；切会话即时刷新（ADR-024）
```

§8 开发路线追加：`22. [x] 迭代增强：TUI 布局调整（ADR-024，设计见 docs/superpowers/specs/2026-08-31-tui-layout-refine-design.md）`。

- [ ] **Step 4: 更新 `code_agent/docs/superpowers/specs/2026-08-31-observability-rename-design.md`**

在文件头部状态行下方追加一行注记：`> 注：状态栏 ctx/cache 展示已由 2026-08-31-tui-layout-refine-design.md 取代（移至底栏、格式改为 213.0k(21%)、分母仍为 W）。`

- [ ] **Step 5: 追加 ADR-024 到 `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`**（`## 后续决策记录处` 之前插入）

```markdown
## ADR-024：TUI 布局调整（顶栏路径/会话名 + 底栏 stats + 切会话刷新）
- **日期**：2026-08-31
- **状态**：已实施
- **背景**：ADR-023 把 ctx/cache 放顶栏，但用户反馈：顶栏看不到完整工作区路径、session 显示 id 而非名称、ctx/cache 应在底栏与快捷键同行最右侧且格式更紧凑；且切会话时 ctx/cache 不刷新（残留上一会话）。
- **决策**：①顶栏 Workspace 显示完整路径（`Workspace.path`）、session 显示会话名（`SessionStore.get_title` + `AgentSession.current_title`，空则 `new`）；②新增 `StatusFooter(Footer)`（右停靠 `#footer-stats`），格式 `213.0k(21%) cache:N%`（恒一位小数、pct 相对上下文窗口 W、启发式 `~`、无缓存隐藏 cache）；③切会话刷新：`load_session` 用 `estimate_tokens` 设启发式 `last_usage`，`new_session` 清空 `last_usage`，`_refresh_status` 同步刷底栏。
- **理由**：信息层次更合理（顶栏=身份，底栏=运行统计）；`get_title` 只读首行 O(1)；StatusFooter 子类化保留标准快捷键样式且 recompose 后子部件存活。
- **影响**：workspace/session/agent/tui/widgets/tui/app 五处改动；`_fmt_k`/`_usage_segments` 删除，改 `_fmt_ctx`/`_footer_stats`；对应测试更新。
```

- [ ] **Step 6: 提交**

```bash
git add docs/architecture.md docs/development.md docs/design.md docs/superpowers/specs/2026-08-31-observability-rename-design.md
git commit -m "docs: 同步 TUI 布局调整文档并记录 ADR-024"
```

---

### Task 6: 全量回归 + 凭据复核

**Files:** 无代码改动。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS（311 + 本迭代新增 ≈ 330 用例）。

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
