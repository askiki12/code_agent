# 迭代设计：Textual TUI 重构（可滚动对话 + 会话列表 + 快捷键）

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-020（实现时追加，supersede ADR-019 渲染方案）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

ADR-019 的 rich TUI 冒烟后暴露两个问题：① 对话区**有界不可滚动**（Panel 内容超出即截断，只能看最近几条）；② 界面朴素（纯文本状态行、无色彩层级）。用户要求**用 Textual 重构**：功能完整（流式、工具行、斜杠命令、权限 ask、会话 resume/切换、滚轮+键盘滚动）且界面美观。

**目标**：`--interactive` 在 TTY 下运行全屏 Textual TUI（Header + 可滚动对话区 + 会话列表面板 + 输入栏 + Footer 快捷键），后台线程跑 agent 循环、UI 始终响应；`run_tui` 签名不变，cli 接线零改动。

**迭代约束（用户决定）**：Textual（新依赖，rich 保留作底层）；`run_task` 用**后台线程 + 异步桥**；功能范围**核心 + 会话列表 + 快捷键**；结构 **`code_agent/tui/` 包拆分**（app/widgets/worker）。

## 2. 范围

**In scope**
- `pyproject.toml` 新增 `textual>=0.80`；`uv sync`。
- 新增 `code_agent/tui/` 包：`__init__.py`（导出 `run_tui` + 纯函数）、`app.py`（`CodeAgentApp`）、`widgets.py`（`StatusBar` / `ConversationLog` / `SessionList` / `PromptInput`）、`worker.py`（`AgentWorker`）。
- 删除单文件 `code_agent/tui.py`（被包取代）。
- 复用：`cli.handle_command`、`session.ask` 注入、`Conversation` 代理项清洗（已修）。
- 测试（Textual `run_test` + 纯函数）、文档同步、ADR-020、真实冒烟。

**Out of scope（本期不做）**
- markdown 渲染（纯文本流式）、鼠标拖拽选字、主题/配色配置、多行输入编辑。
- 子智能体可视化面板、实时 token/成本显示。
- agent 循环硬中断（worker 线程无法安全 kill；标记停止待当前轮返回）。

## 3. 依赖与包结构

- 运行时依赖：`requests>=2.31` + `rich>=13` + `textual>=0.80`（rich 由 textual 依赖，保留直接声明）。
- ADR-020：Textual TUI 重构，supersede ADR-019 的 rich 渲染方案。

```
code_agent/tui/
├── __init__.py      # run_tui(session, store, workspace=None, *, model="")；format_user/format_assistant/format_tool；_append_tool_line；_line_style
├── app.py           # CodeAgentApp(App)：compose、bindings(Ctrl+Q/N/L)、on_input_submitted、worker 生命周期
├── widgets.py       # StatusBar / ConversationLog / SessionList / PromptInput
└── worker.py        # AgentWorker：后台线程跑 run_task，事件经 App.call_from_thread 桥回 UI；ask 经 asyncio.Event 唤醒
```

- `run_tui(...)` 签名与行为保持：cli.py `if _use_tui(): run_tui(session, store, workspace, model=llm.model); return 0` 不变。

## 4. 布局（全屏 alt-screen）

```
┌─ Header ──────────────────────────────────────────────────┐
│ code_agent │ Workspace: <name>(<id>) · model · session     │
│ ● idle(绿) / ● running(黄)                                 │
├────────────────────────────────────────┬───────────────────┤
│ ConversationLog（可滚动）              │ SessionList       │
│   > user: ...              (cyan bold) │ (OptionList)      │
│   [tool] read_file ok      (dim)       │  Ctrl+L 切换显隐   │
│   assistant: ...（流式末行更新）        │                   │
│   [agent] stopped: ...     (yellow)    │                   │
│   [session ...]            (magenta)   │                   │
├────────────────────────────────────────┴───────────────────┤
│ ❯ <Input>（运行中显示 [running…]）                          │
└─ Footer: Ctrl+Q quit · Ctrl+N new · Ctrl+L sessions ────────┘
```

## 5. Widget 职责

| Widget | 职责 |
|---|---|
| `StatusBar` | workspace/model/session + 状态徽章（idle 绿 / running 黄），订阅状态消息更新 |
| `ConversationLog` | `Widget` + `VerticalScroll`：渲染带样式 `Text` 行；`append()` 追加、`update_last()` 更新末行（流式）；默认自动到底，用户滚回时暂停跟随 |
| `SessionList` | `OptionList` 填充 `store.list_sessions()`；选中 → `session.load_session(id)` + 刷新对话区；`Ctrl+L` 切换 |
| `PromptInput` | `Input` 子类：Enter 提交；`/` 命令走 `handle_command`；权限 ask 时临时变确认提示 |

## 6. 快捷键（bindings）

| 键 | 动作 |
|---|---|
| `Ctrl+Q` | 退出（恢复终端） |
| `Ctrl+N` | 新建会话（`session.new_session()` + 清空对话区） |
| `Ctrl+L` | 切换会话列表面板 |
| `Ctrl+C` | 运行中标记停止请求（worker 当前轮返回后停止），否则清空输入 |
| 滚轮 / PageUp / PageDown | 对话区滚动 |

## 7. AgentWorker（后台线程 + 异步桥）

```
run_tui → CodeAgentApp.run()
  ├─ AgentWorker.start(task, on_delta, on_tool, on_done, ask)
  │    └─ threading.Thread(target=session.run_task, args=(task,))
  │        ├─ on_delta  → App.call_from_thread(ConversationLog.update_last)
  │        ├─ on_tool   → App.call_from_thread(ConversationLog.append(tool 行))
  │        └─ 结束      → App.call_from_thread(on_done(RunResult))
  └─ ask 回调：worker 阻塞期间向 UI 发「等待确认」消息 → PromptInput 显示 [permission] 提示
       → 用户回车 → asyncio.Event 唤醒 worker 返回输入
```

- UI 主线程始终响应：滚动、输入、会话切换、快捷键不受 agent 运行阻塞。
- 运行中可打字排队下一任务（当前设计：运行中禁用提交，Enter 仅提示运行中）。

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| `/resume` 不存在的会话 | 对话区显示错误行，不退出 |
| `run_task` 内部异常 / LLM 错误 | agent 循环兜底（不崩溃），对话区显示 reason |
| 会话保存失败 | context 层代理项清洗 + agent finally `except (OSError, ValueError)`（已修） |
| worker 运行中 `Ctrl+Q` | 标记停止；worker daemon 线程随进程退出，终端恢复 |
| 非 TTY / `NO_TUI=1` | 回退纯文本（`_use_tui` 不变） |

## 9. 测试计划

**纯函数（迁移/新增，无 UI）**
1. `format_user/format_assistant/format_tool` 保持原断言（`test_tui.py` 迁移）。
2. `_line_style(line)`：user/assistant/tool ok/tool failed/stopped/session/其它 → 对应样式。

**worker 桥（`test_tui_worker.py`）**
3. `AgentWorker` 用假 session + 假 llm：on_delta/on_tool/on_done 均被调用且内容正确；ask 回调经 Event 唤醒返回输入。

**App 冒烟（`test_tui_app.py`，Textual `run_test`）**
4. 注入假 session：模拟输入任务 → 对话区出现 `> user:` 与 `assistant:` 行。
5. 模拟 `/list` → 对话区出现会话列表行。
6. `Ctrl+L` 切换 SessionList 显隐；`Ctrl+N` 新建会话清空对话。
7. `Ctrl+Q` 正常退出（App 返回）。

**兼容回归**：现有 239 用例全绿（`run_tui` 签名不变，cli 接线不变）。

全部离线（假 llm/假 session）。真实冒烟：TTY 下运行，验证滚动、会话切换、流式与退出。

## 10. 文档同步

- `docs/architecture.md`：模块总览 `code_agent/tui.py` → `code_agent/tui/` 包；§3 接口（run_tui / AgentWorker / widget 职责）。
- `docs/development.md`：依赖（+textual）；`--interactive` TUI 说明更新（滚动/会话列表/快捷键）；测试目录 `test_tui*.py`。
- `docs/design.md`：§6 勾选；§8 追加（17）；测试计数实际值。
- `README.md`：依赖 + TUI 使用（快捷键表）；结构 `tui/`。
- `code_agent/docs/superpowers/specs/2026-08-30-tui-textual-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-020（supersede ADR-019 渲染方案）；`.agent/01` / `.agent/04` 依赖/测试范围同步。

## 11. 开发顺序（小步推进，每步可验证）

1. 依赖：pyproject 加 `textual>=0.80` + `uv sync` + 全量回归（239）
2. `tui/` 包骨架：`__init__.py`（迁移 format_*/run_tui 占位）+ 删除旧 tui.py + 测试迁移（纯函数绿）
3. `_line_style` + widgets：`ConversationLog`（append/update_last/滚动）+ 纯函数测试
4. `AgentWorker` + 桥测试
5. `CodeAgentApp`（compose/bindings/Input 提交/session 切换）+ App 冒烟测试
6. 文档同步 + ADR-020
7. 全量回归 + 真实冒烟（TTY 验证滚动/切换/退出）+ 凭据复核 + 提交
