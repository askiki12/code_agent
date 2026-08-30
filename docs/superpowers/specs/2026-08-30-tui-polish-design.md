# 迭代设计：TUI 打磨——多轮渲染修复、布局重叠修复、子智能体状态、! 指令、移除命令面板

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-021（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

Textual TUI 冒烟后用户反馈三类问题：
1. **2 个 UI bug**：
   - 工具调用行错位——工具行一开始显示在 assistant 输出后面，刷新/切会话后才到正确位置（根因：`_assistant_idx` 钉死第一行，多轮 assistant 文本合并、工具行堆末尾；`_reload_conversation` 按 messages 重建才对）。
   - `Ctrl+L` 切换会话列表面板时，若对话区已滚动且未聚焦，内容不重排直接溢出到会话列表面板区，元素重叠。
2. **3 个改进**：子智能体运行状态标注（轻量）、skill 显示、`!` 输入执行终端命令。
3. **2 处删除**：Ctrl+P 命令面板（含主题切换/截图等），快捷功能提示已足够，无需命令面板。

**目标**：修复多轮渲染与布局重叠；`dispatch_subagent` 运行时对话区标注"子智能体运行中"；`!cmd` 在工作目录执行终端命令并回显输出；移除 Textual 命令面板（Ctrl+P）。

**迭代约束（用户决定）**：子智能体/skill **不回显完整报告**，仅标注运行状态；`!` 命令**跑 shell 回显输出**（超时 120s，用户主动发起，不走 agent/policy）。

## 2. 范围

**In scope**
- `agent.py` `run_task` 新增 `on_assistant_start` / `on_tool_start` 两个可选回调。
- `worker.py` 桥接两回调 → `call_from_thread`。
- `app.py`：多轮渲染（按回合新建 assistant 行并重钉索引）、`action_toggle_sessions` 重排+钳制滚动、`_on_tool_start` 子智能体运行标注、`!` bang 命令、`COMMANDS=()` + `ctrl+p` no-op 移除命令面板。
- `__init__.py` `_line_style`：`[tool] use_skill` / `[tool] dispatch_subagent` 特殊样式。
- 测试、文档同步、ADR-021、真实冒烟。

**Out of scope（本期不做）**
- 子智能体完整报告回显、markdown 渲染、多行输入、主题配置、命令面板自定义命令。
- `!` 命令的权限校验（用户主动发起，绕过 agent/policy）。
- agent 循环硬中断。

## 3. Bug 修复一：多轮渲染错位

### 根因
`_start_task` 预置一行 `"assistant: "` 并 `_assistant_idx` 钉死；`_on_delta` 永远更新该行。agent 循环有**多轮**（assistant 文本 → 工具 → assistant 文本 → 工具…），故所有轮文本合并进第一行、工具行堆末尾；`_reload_conversation` 按 `session.conversation.messages` 重建时恢复真实交错顺序——两者不一致。

### 修复
1. `agent.py` `run_task(self, task, on_delta=None, on_tool=None, on_assistant_start=None, on_tool_start=None)`：
   - 循环内 `response = self.llm.chat(...)` **之前**：`if on_assistant_start is not None: on_assistant_start()`。
   - `for tc in response.tool_calls` 内 `result = self._run_tool(tc)` **之前**：`if on_tool_start is not None: on_tool_start(tc.name)`。
   - 既有 `on_tool`/`on_delta` 语义不变（向后兼容，缺省 None）。
2. `worker.py` `AgentWorker` 增加 `on_assistant_start` / `on_tool_start` 参数并桥接。
3. `app.py`：
   - `_start_task` **不再**预置 `"assistant: "` 行。
   - `_on_assistant_start()`：`log.append("assistant: ")`；`self._assistant_idx = len(log._lines) - 1`。
   - `_on_delta` / `_on_tool` / `_on_done` 逻辑沿用（更新钉住行 / 追加工具行 / 完成时更新 assistant 行）。
   - 刷新/切会话 `_reload_conversation` 不变，两处现一致。

## 4. Bug 修复二：Ctrl+L 布局重叠

### 根因
`#sessions` 从 `display:none` 切可见时，Horizontal 收窄对话区，但 ConversationLog 内嵌 `Static` 渲染缓存于旧宽度、滚动偏移未对新宽度重排 → 内容溢出到会话列表区。

### 修复
`app.py` `action_toggle_sessions`：切换 `visible` 并 `refresh_from` 后，对 `#log`：
1. 强制重渲染 body（`ConversationLog._update_body()`，按新宽度重排）。
2. 钳制滚动偏移到新内容高度（`scroll_offset.y = min(原 y, max_scroll_y)`；以 Textual 8.2.8 实际 API 为准，如 `scroll_to`）。

## 5. 改进一：子智能体运行状态标注（轻量）

- `agent.py` `run_task` 新增 `on_tool_start(name)`（见 §3）。
- `app.py` `_on_tool_start(name)`：
  - `name == "dispatch_subagent"`：append `[subagent] 子智能体运行中…`（黄），记住索引 `self._subagent_idx`。
  - `_on_tool(name, res)` 中若 `name == "dispatch_subagent"`：更新该行为 `[subagent] ✓ 完成`。
- skill：`_line_style` 对 `[tool] use_skill`（品红）与 `[tool] dispatch_subagent`（青）特殊样式，不改回显内容。

## 6. 改进二：`!` 终端命令

- `on_input_submitted`：`value` 以 `!` 开头（`len > 1`）→ `_run_bang(value[1:].strip())`；空则忽略。
- `_run_bang(cmd)`：后台线程 `subprocess.run(cmd, shell=True, cwd=self.session.workdir, capture_output=True, text=True, timeout=120)`；经 `call_from_thread` 回显：
  ```
  $ <cmd>
  <stdout> + <stderr>
  [exit <code>]
  ```
- 超时 → `[command timed out after 120s]`；`OSError` 等 → `[command failed: <err>]`。
- **并发语义（明确）**：`!` 在 agent 空闲时执行；若 `_busy()`（agent 或另一条 `!` 正在运行）→ 拒绝并 `notify("任务运行中")`。`!` 运行期间同样置 busy（输入栏提示运行中），完成后恢复。`!` 命令不落会话持久化（纯临时回显）。
- 复用 `AgentWorker` 的桥模式（独立线程 + `call_from_thread`）。

## 7. 删除：Ctrl+P 命令面板

- `app.py`：
  - `COMMANDS = ()`（清空 Textual 命令面板，主题切换/截图等消失）。
  - `BINDINGS` 显式绑定 `Binding("ctrl+p", "noop", "disabled")` 覆盖默认（`action_noop` 为空操作）。
- Footer 仅保留 `Ctrl+Q quit · Ctrl+N new · Ctrl+L sessions`。

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| `!` 命令超时（>120s） | 回显 `[command timed out after 120s]` |
| `!` 命令 OSError / 非零退出 | 回显 stderr + `[exit <code>]`（ok 语义不崩溃） |
| `dispatch_subagent` 运行中用户 `Ctrl+Q` | 沿用现有 busy 守卫（运行中会话动作拒绝） |
| 多轮渲染中 `_on_delta` 索引越界 | 沿用既有越界防护（`0 <= idx < len`） |
| 布局切换异常 | 钳制滚动 try/except 兜底（不影响功能） |

## 9. 测试计划（离线）

**test_agent.py（扩展）**
1. `on_assistant_start`：FakeLLM 两轮（read_file 工具 + 终答）→ 回调被调用 2 次；`on_tool_start` 收到 `"read_file"`。
2. 缺省 `None`：既有行为不变（兼容回归）。

**test_tui_worker.py（扩展）**
3. AgentWorker 桥接 `on_assistant_start`/`on_tool_start` → `call_from_thread` 被调用。

**test_tui_app.py（扩展）**
4. 多轮渲染：注入 FakeLLM（一轮"工具前文本 + read_file 调用 + 终答"）→ 断言对话区顺序：`> user:` → 第一行 `assistant:`（含工具前文本）→ `[tool] read_file` 行 → 新 `assistant:` 行（终答）。断言 `_assistant_idx` 指向终答行。
5. `_on_tool_start`：dispatch_subagent 名称 → 对话区出现 `[subagent] 子智能体运行中…`；on_tool 后变 `[subagent] ✓ 完成`。
6. `!` bang：`_run_bang` 用 monkeypatch `subprocess.run` 返回假结果 → 对话区出现 `$ cmd`、输出、`[exit 0]`；超时路径 → `[command timed out after 120s]`。

**test_tui.py（扩展）**
7. `_line_style`：`[tool] use_skill ...` → 品红；`[tool] dispatch_subagent ...` → 青。

**布局重叠修复验证**：run_test 难以断言像素级布局，故 §4 的重排+钳制滚动以真实冒烟为准（用户在终端验证 `Ctrl+L` 滚动后切换无重叠）；自动化测试覆盖其不抛异常（`action_toggle_sessions` 冒烟）。

**兼容回归**：现有 252 用例全绿。

全部离线。真实冒烟：用户终端跑 `--interactive`，验证多轮工具行顺序、Ctrl+L 无重叠、`!pwd` 回显、Ctrl+P 无面板、子智能体运行标注。

## 10. 文档同步

- `docs/architecture.md`：`run_task` 签名补 `on_assistant_start`/`on_tool_start`；TUI 说明补 `!` 命令、子智能体运行标注、命令面板移除。
- `docs/development.md`：`--interactive` 说明补 `!` 用法、快捷键表（去掉 Ctrl+P）；测试目录说明。
- `docs/design.md`：§6 勾选；§8 追加（18）；测试计数实际值。
- `README.md`：TUI 快捷键表去 Ctrl+P、补 `!`；计数实际值。
- `code_agent/docs/superpowers/specs/2026-08-30-tui-polish-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-021（不入库）。

## 11. 开发顺序（小步推进，每步可验证）

1. `agent.py`：`on_assistant_start` / `on_tool_start` 回调 + `test_agent.py`（TDD）
2. `worker.py`：桥接两回调 + `test_tui_worker.py`（TDD）
3. `app.py`：多轮渲染重构（`_start_task`/`_on_assistant_start`/`_on_delta`）+ `test_tui_app.py` 多轮用例（TDD）
4. `app.py`：`_on_tool_start` 子智能体运行标注 + `_line_style` skill/subagent 样式 + 测试（TDD）
5. `app.py`：`!` bang 命令 + 测试（TDD）
6. `app.py`：`action_toggle_sessions` 重排+钳制滚动 + 移除命令面板（COMMANDS=() + ctrl+p noop）+ 测试
7. 文档同步 + ADR-021
8. 全量回归 + 真实冒烟 + 凭据复核 + 提交
