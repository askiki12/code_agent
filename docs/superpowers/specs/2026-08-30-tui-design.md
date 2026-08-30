# 迭代设计：TUI 终端界面（rich，--interactive 升级）

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-019（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

agent 功能已完善（10 工具、会话/权限/skill/子智能体），但交互入口仍是纯 `input()` 循环 + 流式 print，观感朴素，不利于演示视频与日常使用。业界（Claude Code / OpenCode）均以 TUI 承载交互。本迭代为 `--interactive` 增加 **rich 驱动的 TUI**：状态栏 + 流式对话区 + 输入栏，TTY 自动进入、非 TTY 回退纯文本。

**目标**：`code_agent --interactive` 在 TTY 下呈现布局化、流式刷新的终端界面；权限 ask 在输入栏确认；斜杠命令可用；无 TTY 时行为不变。

**迭代约束（用户决定）**：技术栈 **rich**（新增运行时依赖，记 ADR-019）；布局 **状态栏+对话区+输入栏**（无工具侧栏/多面板）；接入 **--interactive 直接升级**（非 TTY 回退纯文本）；运行模型 **单线程 rich Live**（运行中禁用输入，跑完恢复）。

## 2. 范围

**In scope**
- `pyproject.toml` 新增依赖 `rich>=13`；`uv sync` 更新 `uv.lock`。
- 新增 `code_agent/tui.py`：`run_tui(...)` + 对话格式化纯函数 + ask 渲染回调。
- `permissions.py`：`Policy.check` 新增可选 `ask` 回调（缺省走现有 `input()`）。
- `agent.py`：`AgentSession.__init__` 新增 `ask` 参数，`_run_tool` 透传给 `policy.check`。
- `cli.py`：拆出纯函数 `handle_command`（返回退出标志+输出行）；`--interactive` TTY→TUI、非 TTY→纯文本。
- 测试、文档同步、ADR-019、真实冒烟（TUI 交互一次）。

**Out of scope（本期不做）**
- 工具侧栏/多面板、鼠标交互、主题配置、全文滚动快捷键（仅自动到底）。
- 子智能体可视化面板；后台线程/异步。
- TUI 下新增会话/工作区管理功能（沿用现有斜杠命令）。

## 3. 依赖与架构

- 运行时依赖 `rich>=13`（终端渲染库，非 agent 框架/SDK，符合红线；理由见 ADR-019）。
- 新模块 `code_agent/tui.py`：
  ```
  tui.py（依赖 rich + agent/session/workspace 接口）
  ├── run_tui(session, store, workspace=None, *, model="") -> None
  │     # 主入口：rich Layout（状态栏/对话区/输入栏），Live 上下文内跑交互循环
  ├── format_assistant(role, content) -> str          # 纯函数：对话行文本
  ├── format_user(content) -> str
  ├── format_tool(name, ok, truncated, output) -> str # 纯函数：紧凑工具行
  └── _ask_renderer() -> Callable[[str], str]          # 权限 ask 回调：输入栏确认
  ```
- `run_tui` 交互循环（单线程）：
  1. 渲染初始状态（workspace/会话信息）。
  2. 等待输入：普通任务 → `session.run_task(task, on_delta=...)`，`on_delta` 追加到对话区并 `Live.refresh()`；运行期间输入栏显示 `[running…]` 禁用。
  3. 斜杠命令 → `handle_command`，输出行进对话区。
  4. `Ctrl+C`/`/exit` → 优雅退出（`Live.stop()` 后打印会话 id）。

## 4. 布局与交互

```
┌─ 状态栏 ─────────────────────────────────────────────────┐
│ Workspace: <name> (<id>) | model: <model> | status: idle/running │
├─ 对话区（自动到底）─────────────────────────────────────┤
│ > user: <任务>                                            │
│ [tool] read_file ok: tests/test_foo.py (5 lines)          │
│ assistant: <流式追加>                                     │
├─ 输入栏 ─────────────────────────────────────────────────┘
│ > _                                                        │
```

- 输入栏：普通任务回车执行；斜杠 `/new` `/list` `/resume <id>` `/exit`；运行中显示 `[running…]`。
- 工具调用紧凑渲染：`[tool] <name> ok|failed (truncated) | <输出首行>`。
- 权限 ask：ask 回调把输入栏临时变为 `[permission] <tool>(<args 前 60 字符>) allowed? [y/N]`，读一行后返回，`y`→allow 其余→deny（与现有语义一致）。
- 会话保存：`run_task` 结束后显示 `[session <id>]`。

## 5. 权限 ask 注入（核心集成点）

- `permissions.py`：
  ```python
  def check(self, tool: str, arguments: dict, interact: bool = False, ask=None) -> PermissionResult:
      ...
      if interact:
          prompt = f"[permission] {tool}({text[:60]!r}) allowed? [y/N] "
          decision = "allow" if (ask if ask is not None else input)(prompt).strip().lower() == "y" else "deny"
  ```
- `agent.py`：`__init__` 新增 `ask: Callable[[str], str] | None = None`；`_run_tool` 中 `self.policy.check(tc.name, tc.arguments, interact=self.interact, ask=self.ask)`。
- 缺省（`ask=None`）→ `input()`，纯文本行为完全不变；TUI 注入渲染回调。
- 子智能体：`_dispatch_subagent` 构造子会话时透传 `ask=self.ask`（子会话权限 ask 同样走注入回调）。

## 6. CLI 集成与回退

- `cli.py`：`_handle_command` 重构为纯函数 `handle_command(cmd, session, store) -> tuple[bool, list[str]]`（返回退出标志 + 输出行；不再 print），cli 与 tui 共用。
- `main()` 的 `--interactive` 分支：
  ```python
  if sys.stdout.isatty() and os.environ.get("NO_TUI") is None:
      from code_agent.tui import run_tui
      run_tui(session, store, workspace, model=llm.model)
  else:
      <现有纯文本交互循环>
  ```
- `--prompt` / `--list-sessions` / `--resume` 行为不变。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| 会话加载失败（/resume 不存在） | 输入栏/对话区显示错误行，不退出 |
| `run_task` 内部异常 | 由 agent 循环兜底（不崩溃），对话区显示终止原因 |
| `Ctrl+C` | 优雅退出：Live 停止、终端恢复、打印当前会话 id |
| 非 TTY 启动 | 自动回退纯文本交互（现有逻辑） |
| 未知斜杠命令 | 对话区显示 `unknown command` |

## 8. 测试计划（离线为主）

**test_cli.py（扩展）**
1. `handle_command` 纯函数：/new、/list、/resume、/exit、未知命令 → 正确的 `(bool, list[str])`。

**test_tui.py（新增）**
2. `format_user` / `format_assistant` / `format_tool`：三类消息格式化正确（工具行含 `[tool]`、ok/failed、truncated、首行）。
3. `format_tool`：长输出只取首行；截断标记。
4. `run_tui` 冒烟：monkeypatch `rich.live.Live` 为桩 + 假 session（FakeLLM），喂一次输入后退出，断言未抛异常且调用过 `run_task`。

**test_permissions.py（扩展）**
5. `Policy.check(..., interact=True, ask=fake_ask)`：用 fake 回调而非 input（y→allow，n→deny）。
6. 不传 ask → 走默认 input（monkeypatch `builtins.input` 断言被调用）。

**兼容性**：现有 219 用例全绿（`Policy.check` 签名向后兼容）。

全部离线。冒烟（真实 API）：TTY 下 `--interactive` 进入 TUI，提交一个真实任务（如"列出当前目录"），观察流式输出与工具行，随后 `/exit` 退出。

## 9. 文档同步

- `docs/architecture.md`：模块总览加 `tui.py`；§3 接口约定（run_tui / format_* / Policy.ask / AgentSession.ask / handle_command）。
- `docs/development.md`：运行方式更新（`--interactive` 进入 TUI，`NO_TUI=1` 回退）；测试目录说明加 `test_tui.py`。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加（16）。
- `README.md`：交互式使用说明更新（TUI 布局、快捷键、NO_TUI）；依赖更新（runtime 依赖 rich）。
- `code_agent/docs/superpowers/specs/2026-08-30-tui-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-019（不入库，ADR-007）；`.agent/01` / `.agent/04` 若涉及依赖/测试范围同步。

## 10. 开发顺序（小步推进，每步可验证）

1. 依赖：pyproject 加 `rich>=13` + `uv sync` + 全量回归（219 全绿）
2. `permissions.py` ask 注入 + `agent.py` ask 透传（TDD：ask 回调测试 + 兼容性回归）
3. `cli.py` `handle_command` 纯函数化（TDD：handle_command 测试）
4. `tui.py` 格式化纯函数（TDD：format_user/assistant/tool 测试）
5. `tui.py` `run_tui` + CLI 接线（TDD：run_tui 冒烟 + isatty/NO_TUI 分支测试）
6. 文档同步 + ADR-019
7. 真实冒烟（TUI 交互一次）+ 全量回归 + 凭据复核 + 提交
