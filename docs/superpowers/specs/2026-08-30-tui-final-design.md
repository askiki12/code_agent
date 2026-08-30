# 迭代设计：TUI 收尾——! 命令模式、Ctrl+P 隐藏、skill 面板与标注

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-022（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

TUI 打磨迭代后又收到三点反馈 + 一项新需求：
1. 输入 `!` 时输入框无任何视觉变化，用户无法感知已进入 shell 命令模式。
2. `^P` 命令面板虽已禁用，但 Footer 仍显示 `ctrl+p disabled`，冗余。
3. skill 显示此前只做了工具行样式，未做运行标注；且用户要求 **Ctrl+S 查看可用技能 + 选中回车让智能体使用**（或经提示词使用）。

**目标**：`!` 命令模式 UI 反馈；Ctrl+P 从 Footer 彻底隐藏；skill 加载标注（`[skill] 加载 <name>…/✓/✗`）；Ctrl+S 技能列表面板（选中回车→派发"使用技能 <name>"任务）。

**迭代约束（用户决定）**：skill 面板与子智能体一致走**轻量标注**（不回显 SKILL.md 全文）；选中技能回车用当前输入框内容当任务（空则"加载并说明其用法"）；提示词方式使用 skill 已支持不改动。

## 2. 范围

**In scope**
- `PromptInput`：`on_input_changed` 切 `command-mode` 类 + placeholder 切换。
- `app.py`：`Binding("ctrl+p", ..., show=False)`；`SkillList` 部件；右侧列改 `Vertical(SessionList, SkillList)`；`action_toggle_skills`（Ctrl+S）；`on_option_list_option_selected` 按来源路由（sessions/skills）；技能选中派发任务。
- `widgets.py`：`SkillList(OptionList)`，`refresh_from(registry)`。
- `agent.py`/`worker.py`：`on_tool_start(name, arguments)` 扩展（传入参数，供 skill 名）。
- `__init__.py` `_line_style`：`[skill]` 行品红。
- 测试、文档同步、ADR-022、真实冒烟。

**Out of scope（本期不做）**
- skill 面板搜索/过滤；多选；skill 附带脚本执行。
- `!` 命令的更多模式（仅命令模式 UI 反馈，执行语义不变）。

## 3. `!` 命令模式 UI 反馈

- `PromptInput.on_input_changed`：
  - `value.startswith("!")` → `self.set_class(True, "command-mode")`；placeholder → `"❯ shell: 输入命令（回车执行）"`。
  - 否则 → `self.set_class(False, "command-mode")`；placeholder 还原 `"❯ 输入任务（/ 开头为命令）"`。
- `app.py` CSS：`#input.command-mode { border: round $warning; }`。
- 执行语义不变（`!cmd` 仍走 `_run_bang`）。

## 4. Ctrl+P 彻底隐藏

- `BINDINGS` 中 `Binding("ctrl+p", "noop", "disabled", show=False)`——Footer 不显示该绑定；按键仍被拦截（命令面板已由 `ENABLE_COMMAND_PALETTE=False` 禁用）。

## 5. skill 加载标注

- `agent.py` `run_task`：`on_tool_start` 签名改为 `on_tool_start(name, arguments)`（`tc.arguments` 一并传入）。
- `worker.py`：`_tool_start(name, args)` 桥接。
- `app.py`：
  - `_on_tool_start(name, args)`：`use_skill` → `[skill] 加载 <args.get('name')>…`（品红）并 pin `self._skill_idx`；`dispatch_subagent` 逻辑不变。
  - `_on_tool(name, res)`：`use_skill` → 更新 `[skill] ✓ <name>` / `✗ <name>`。
- `__init__.py` `_line_style`：`[skill]` 行含 `✓` → `"green"`、含 `✗` → `"red"`、否则 `"magenta"`。

## 6. Ctrl+S 技能选择弹窗（模态）

> 用户裁决：不用右侧边栏（避免与 Ctrl+L 会话侧栏双面板拥挤/布局冲突），改用**模态弹窗**——Ctrl+S 弹出居中技能选择框，方向键选择、回车使用、**Esc 退出**。

- `app.py` 新增 `SkillScreen(Screen)`：
  - `compose()`：居中 `Panel`（标题"可用技能"）内含 `OptionList`（`SkillList` 部件，`refresh_from(registry)` 填充 `Option(f"{name}  {description}", id=name)`）。
  - `BINDINGS`：`Binding("escape", "dismiss", "Cancel")`——Esc 关闭；回车/选中由 OptionList 的 `OptionSelected` 处理。
  - `on_option_list_option_selected`：`self.dismiss(event.option.id)`（技能名）。
- `app.py`：
  - `BINDINGS` 追加 `Binding("ctrl+s", "toggle_skills", "Skills")`（或 `action_choose_skill`）。
  - `action_choose_skill`：busy 守卫；`session.skills` 为空 → `notify("无可用技能")`；否则 `self.push_screen(SkillScreen(registry), callback=self._on_skill_chosen)`。
  - `_on_skill_chosen(name)`（Screen 关闭后回调）：
    - `name is None`（Esc 取消）→ 不动作；
    - 否则：输入框有值（`#input.value.strip()`）→ `task = f"请使用技能 {name} 完成：{input_text}"`；空 → `task = f"请加载技能 {name} 并按其指令说明能做什么"`；清空输入框 → `_start_task(task)`。
- 右侧列布局保持 `Horizontal(ConversationLog, SessionList)`（SessionList 单独在右，无冲突）。
- 提示词方式使用 skill：已有（agent 遇提示自行调 `use_skill`），不改动。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| 无可用技能按 Ctrl+S | `notify("无可用技能")`，不开弹窗 |
| 运行中按 Ctrl+S / 选择技能 | busy 守卫拒绝（notify） |
| Esc 关闭弹窗 | `dismiss(None)`，不派发任务 |
| 技能名含特殊字符 | 仅作字符串拼入任务，无路径面 |
| `!` 模式输入清空/改回 | `on_input_changed` 还原类与 placeholder |

## 8. 测试计划（离线）

**test_tui_widgets.py（扩展）**
1. `SkillList.refresh_from`：假 registry（含 2 技能）→ `option_count == 2`；空 registry → 0。
2. `PromptInput` 命令模式：设值 `!ls` 触发 changed → `has_class("command-mode")` + placeholder 含 `shell:`；改回 `ls` → 还原。

**test_tui_app.py（扩展）**
3. `Binding("ctrl+p", ..., show=False)`：断言该 binding 的 `show is False`。
4. `action_choose_skill`：有技能 → `push_screen(SkillScreen)` 被调用（冒烟不崩）；无技能 → notify。
5. 技能选中：注入含 skill 的假 registry + FakeLLM → Ctrl+S 打开弹窗 → 选中技能 → `_on_skill_chosen(name)` 回调 → 断言对话区出现 `> user: 请使用技能 <name> 完成：`（或加载说明），且 FakeLLM 收到该任务。
6. Esc 关闭：打开弹窗 → press escape → 弹窗关闭，无任务派发。
7. `on_tool_start` 含参：use_skill 工具调用 → 对话区出现 `[skill] 加载 <name>…` → on_tool 后 `[skill] ✓ <name>`。

**test_tui.py（扩展）**
7. `_line_style`：`[skill] 加载 x…` → 品红；`[skill] ✓ x` → 绿；`[skill] ✗ x` → 红。

**test_agent.py（扩展）**
8. `on_tool_start` 收到 `(name, arguments)`（工具参数）。

**兼容回归**：现有 263 用例全绿。

全部离线。真实冒烟：`!ls` 输入框变色；`Ctrl+P` Footer 无显示；配置示例 skill 后 `Ctrl+S` 查看 + 选中回车 agent 使用；提示词让 agent 用 skill。

## 9. 文档同步

- `docs/architecture.md`：`run_task` on_tool_start 签名（name, arguments）；TUI 补 `!` 命令模式、Ctrl+S 技能面板、skill 标注。
- `docs/development.md`：快捷键表加 Ctrl+S（技能选择弹窗）；`!` 命令模式说明。
- `docs/design.md`：§6 勾选；§8 追加（19）；测试计数实际值。
- `README.md`：快捷键表加 Ctrl+S（技能弹窗）；skill 面板说明；计数实际值。
- `code_agent/docs/superpowers/specs/2026-08-30-tui-final-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-022（不入库）。

## 10. 开发顺序（小步推进，每步可验证）

1. `agent.py`/`worker.py`：`on_tool_start(name, arguments)` 扩展 + 测试（TDD）
2. `__init__.py` `_line_style` `[skill]` 样式 + `app.py` skill 加载标注（TDD）
3. `widgets.py` `SkillList` + `PromptInput` 命令模式 + 测试（TDD）
4. `app.py`：Ctrl+P show=False + `SkillScreen` 模态弹窗（Ctrl+S 打开/Esc 关闭/选中回调派发）+ 测试（TDD）
5. 文档同步 + ADR-022
6. 全量回归 + 真实冒烟 + 凭据复核 + 提交
