# 迭代设计：TUI 布局调整（顶栏路径/会话名 + 底栏 stats + 切会话刷新）

> 日期：2026-08-31 ｜ 状态：已批准 ｜ 关联 ADR：ADR-024（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

上一迭代（ADR-023）把 ctx/cache 放进了顶栏 StatusBar，但用户反馈布局与信息层次不理想：
1. 顶栏 Workspace 只显示 `name (id)`，看不出工作目录的完整路径；
2. 顶栏 session 显示的是 session id，不是易读的会话名（标题）；
3. ctx/cache 应放在**底部与命令快捷键同一行**的最右侧，而非顶栏；且格式要更紧凑（`213.0k(21%)`，无需 "ctx" 前缀与完整上下文窗口值）；
4. **Bug**：切换会话（恢复历史会话）时，ctx/cache 不刷新，显示的是上一个会话的残留值。

**目标**：顶栏显示完整工作区路径与会话名；ctx/cache 移到底栏（与快捷键同行的最右侧），紧凑格式；切会话时 ctx/cache 立即刷新为该会话的占用。

## 2. 迭代约束（用户决定）

- **切会话刷新语义**：`load_session` 时用 `estimate_tokens` 对已加载对话算启发式 prompt token 数，立即显示该会话的 ctx（带 `~` 表示估算）；`new_session` 清空 `last_usage`，不显示 stats。
- **格式**：`213.0k(21%)` 恒显一位小数（`_fmt_ctx(n) = f"{n/1000:.1f}k"`，即 213000→"213.0k"、90000→"90.0k"）；`pct = int(prompt / W * 100)`（W 为上下文窗口，沿用上迭代决定）；cache 段 `cache:N%`（`cached_tokens>0` 才显示，否则整段隐藏）；启发式加 `~` 前缀。
- **底栏实现**：`StatusFooter` 纯 Widget **render 自绘**（左快捷键 + 右 stats），不子类化 Footer（实施发现 Footer compose 覆写会破坏快捷键渲染，见 §5.1 注）。

## 3. 范围

**In scope**
- `workspace.py`：新增 `Workspace.path` property（返回 `data["path"]`）。
- `session.py`：新增 `SessionStore.get_title(session_id) -> str`（只读首行 meta，坏文件/缺失返回 ""）。
- `agent.py`：`AgentSession.current_title()`；`load_session` 设置启发式 `last_usage`；`new_session` 清空 `last_usage`。
- `tui/widgets.py`：`StatusBar.update_status` 去掉 usage/context_window 参数、改收 session_title；删除 `_usage_segments`/`_fmt_k`；新增 `_fmt_ctx`/`_footer_stats`；新增 `StatusFooter`（render 自绘，左快捷键右 stats + `update_stats(text)`）。
- `tui/app.py`：`compose()` 用 `StatusFooter` 替换 `Footer()`；`_refresh_status` 同步刷底栏 stats；`_workspace_line` 显示完整路径；`_refresh_status` 传 session 标题。
- 测试、文档同步、ADR-024。

**Out of scope（本期不做）**
- 底栏 stats 的宽度自适应降级（文本超宽直接裁剪，不额外压缩）。
- 精确 token 统计/计费。
- 顶栏路径的中间省略（`…/code_agent`）——本期完整显示，靠终端裁剪。

## 4. 顶栏（StatusBar）改造

- `Workspace.path`：返回 `self._data["path"]`（`_create` 已存 realpath）。
- `SessionStore.get_title(session_id)`：
  ```
  path 存在且首行 meta 合法 → 返回 meta["title"]（缺省 ""）
  否则 → ""
  ```
  实现复用 `_read_meta`（只读首行），O(1)。
- `AgentSession.current_title()`：
  - `session_id is None` 或 `store is None` → `""`
  - 否则 → `store.get_title(session_id)`
- `StatusBar.update_status(state, model="", session_title="", workspace_line="")`：
  - 顶栏组装：`workspace_line | model | session: <session_title or "new"> | ● state`。
  - 移除 usage/context_window 逻辑（迁往底栏）。
- `CodeAgentApp._workspace_line()`：`f"Workspace: {self.workspace.path}"`（无 workspace 时 `""`）。
- `CodeAgentApp._refresh_status`：传 `session_title=self.session.current_title()`。

## 5. 底栏（StatusFooter）Stats

### 5.1 控件

`tui/widgets.py` 新增（**实施修正：render 自绘方案，原 Footer 子类方案实测破坏快捷键渲染，见下注**）：

```
class StatusFooter(Widget):
    DEFAULT_CSS: dock bottom, height 1, background $footer-background
    __init__: self._stats_text = ""
    update_stats(text): self._stats_text = text; self.refresh()
    render(): Text = 左段(快捷键文本) + 右对齐 stats（_binding_text 从 screen.active_bindings 取 show=True 的绑定，get_key_display 得键名）
```

- 左侧渲染快捷键（`^q Quit ^n New …`），右侧右对齐紧凑 stats，与快捷键同排共存。
- **注（2026-08-31 实施发现）**：`StatusFooter(Footer)` 子类 + compose 覆写（先 yield `#footer-stats` 再 `yield from super().compose()`）实测导致 FooterKeys **不绘制**（子部件存在但不渲染，纯 `render()` 返回 Blank）。根因是 Footer 为 compose 型 ScrollableContainer，任何在其 compose 前置额外子部件的覆写都会破坏快捷键布局。改为纯 `Widget` 的 render 自绘，快捷键文本自行从 `screen.active_bindings` 渲染。

### 5.2 纯函数

```
_fmt_ctx(n: int) -> str:  f"{n/1000:.1f}k"   # 213000→"213.0k", 90000→"90.0k"
_footer_stats(usage, context_window) -> str:
    usage is None → ""
    prefix = "~" if usage.heuristic else ""
    pct = int(prompt / denom * 100)  (denom = context_window or prompt; denom=0 → 0)
    parts = [f"{prefix}{_fmt_ctx(prompt)}({pct}%)"]
    if not heuristic and cached_tokens > 0: parts.append(f"cache:{int(cached/prompt*100)}%")
    → " ".join(parts)   # 例: "213.0k(21%) cache:40%" / "~12.3k(1%)"
```

### 5.3 app 接线

- `compose()`：`yield StatusFooter(id="footer")`（替换 `Footer()`）。
- `_refresh_status(state)`：除更新顶栏外，同时 `self.query_one("#footer", StatusFooter).update_stats(_footer_stats(self.session.last_usage, self.session.context_window))`。
- `_on_stats(usage)` 仍走 `_refresh_status("running")` → 底栏实时刷新。

## 6. 切会话 ctx/cache 刷新（Bug 修复）

- `AgentSession.load_session(session_id)`：加载对话后：
  ```
  self.last_usage = Usage(prompt_tokens=sum(estimate_tokens(str(m.get("content",""))) for m in self.conversation.messages), heuristic=True)
  ```
- `AgentSession.new_session()`：`self.last_usage = None`。
- 恢复路径：app `on_option_list_option_selected` → `load_session` → `_reload_conversation()` + `_refresh_status("idle")`（刷新顶栏 + 底栏）。`action_new_session` → `new_session` → `_refresh_status("idle")`。两处均已覆盖，无需新增调用。
- CLI 文本模式：`/resume` 后 `last_usage` 同样就绪（虽不展示，语义一致）。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| workspace 为 None | 顶栏无 Workspace 段 |
| `current_title()` store 缺失/会话不存在 | `get_title` 返回 "" → 显示 `session: new` |
| 新会话未运行过 | `last_usage is None` → 底栏不显示 stats |
| 启发式估算 | 底栏 `~` 前缀 |
| 无 cached 数据 | cache 段隐藏 |
| `context_window` 为 0 或未解析 | `denom = context_window or prompt`；`pct = int(prompt / denom * 100) if denom else 0` |

## 8. 测试计划（全部离线）

- `test_workspace.py`：`Workspace.path` == realpath。
- `test_session.py`：`get_title` 正常/缺失/坏文件 → ""。
- `test_agent.py`：`load_session` 后 `last_usage.heuristic is True` 且 `prompt_tokens > 0`；`new_session` 后 `last_usage is None`；`current_title` 有会话名 / 无 id / 无 store。
- `test_tui_widgets.py`：`_fmt_ctx`（213000→"213.0k"、90000→"90.0k"）；`_footer_stats`（None→""、正常含 cache、无 cache 省略、启发式 `~`）；`StatusBar.update_status` 渲染完整路径与 session 标题；`StatusFooter` render 渲染快捷键与 stats（`update_stats` 后 `render().plain`）。删除 `_usage_segments`/`_fmt_k` 相关旧用例。
- `test_tui_app.py`：切会话（on_option_list_option_selected）后底栏 stats 更新为该会话启发式值；Ctrl+N 后底栏无 stats。
- 回归：全量 `uv run pytest` 全绿 + `uv run python -m code_agent --help` 正常 + 凭据 grep 复核。

## 9. 文档同步

- `docs/architecture.md`：workspace.py（path）、session.py（get_title）、agent.py（current_title/load/new 的 last_usage 语义）、tui widgets（StatusBar 签名 / StatusFooter / _footer_stats）、app.py（compose/_refresh_status）。
- `docs/development.md`：TUI 行为段更新（顶栏路径+会话名；底栏 stats 格式）。
- `docs/design.md`：§6 功能范围勾选 + §8 开发路线追加。
- `docs/superpowers/specs/2026-08-31-observability-rename-design.md`：状态栏 ctx/cache 段落标注被本 spec 取代（分母语义不变，展示位置/格式变更）。
- 工作区根 `.agent/03-decisions.md`：ADR-024（不入库，ADR-007）。

## 10. 开发顺序（小步推进，每步可验证）

1. `workspace.py` path + `session.py` get_title + 各自测试（TDD）
2. `agent.py` current_title / load 启发式 last_usage / new 清空 + 测试（TDD）
3. `tui/widgets.py` `_fmt_ctx`/`_footer_stats`/`StatusBar` 改签名/`StatusFooter` + 测试（TDD）
4. `tui/app.py` 接线（compose / _refresh_status / _workspace_line / session 标题）+ 测试（TDD）
5. 文档同步 + ADR-024
6. 全量回归 + 凭据复核 + 提交
