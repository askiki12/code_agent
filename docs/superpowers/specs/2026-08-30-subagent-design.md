# 迭代设计：子智能体派遣（dispatch_subagent，子智能体阉割派遣能力）

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-018（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/tools.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

agent 是单循环单上下文：面对可分解任务（调研+改码、分模块实现），只能串行自己做完，子任务结果全堆在父上下文里。业界（Claude Code subagent、OpenCode Task）以子智能体做编排。本迭代为 agent 增加**子智能体派遣**：父 agent 用 `dispatch_subagent` 把子任务派给一个独立循环的子智能体执行，子智能体跑完自己回合后只把最终报告回传父级（上下文隔离）。**子智能体被阉割派遣能力**（不能再派子代，深度恒为 1），但保留全部其它能力与工具。

**目标**：父 agent 可派遣一个同步子智能体完成子任务，拿回精简报告，全程 harness 强制阉割、无需模型自觉。

**迭代约束（用户决定）**：执行模型为**同步嵌套循环**（一次调用一个子智能体，跑完返回）；工具接口**仅 `task`** 一个参数；实现结构为 **AgentSession 加 `allow_subagent` 参数**。

## 2. 范围

**In scope**
- `agent.py`：`_DISPATCH_SUBAGENT_SCHEMA`、`SUBAGENT_MAX_ITERATIONS=10`、`SUBAGENT_PROMPT_EXTRA`；`AgentSession.__init__` 新增 `allow_subagent: bool = True`；`_run_tool` 分支 + `_dispatch_subagent` 方法。
- 阉割双层强制：子会话工具列表不含派遣 schema + 运行时拒绝。
- 子会话继承：workdir / llm / policy / interact / skills / max_context_tokens / debug；无 store / workspace。
- 测试（mock 模型全离线）、文档同步、ADR-018、真实 API 冒烟。

**Out of scope（本期不做）**
- 并行多子智能体（threading）。
- 子智能体回传中间结果/工具轨迹（仅最终报告，YAGNI）。
- 子智能体结果结构化 schema（纯文本报告）。
- 多层嵌套（子代再派子代——被阉割，深度恒 1）。
- 会话 checkpoint 中恢复子智能体状态。

## 3. 工具接口（`dispatch_subagent`）

```
name: dispatch_subagent
description: Delegate a sub-task to a subagent that runs its own agent loop with all tools except subagent dispatch. Returns the subagent's final report.
参数:
  task (string, 必填): 子任务描述（父级应写自包含、可独立完成的说明）
```

- 返回 `ToolResult`（`ok=true`）output：
  - 子智能体 `RunResult.final_text`（有内容时）；
  - 若子智能体未完成（`finished=False`）：在 final_text 后追加 `\n\n[subagent status: <reason>]`；
  - 若 final_text 为空：`(subagent returned no report; status: <reason>)`。
- 整体经 `truncate()`（8000 字符）截断并标记 `truncated`。
- 失败（缺 task / 内部异常）→ `ok=false`，说明原因。

## 4. 阉割模型（harness 双层强制）

1. **不给 schema**：子会话 `run_task` 传给 `llm.chat` 的 `tools = TOOL_SCHEMAS + ([use_skill] if skills)`，**不含** `_DISPATCH_SUBAGENT_SCHEMA`。
2. **运行时拒绝**：子会话 `_run_tool` 遇 `dispatch_subagent` 返回 `ToolResult(ok=False, output="subagent dispatch is disabled for subagents")`（防模型硬猜工具名）。

子智能体保留全部其它工具与能力：read_file / write_file / edit_file / list_dir / run_command / glob / grep / web_fetch / web_search / use_skill。

## 5. agent.py 设计（新增/改动）

### 常量（模块级）
```python
SUBAGENT_MAX_ITERATIONS = 10

SUBAGENT_PROMPT_EXTRA = (
    "\n\nYou are a subagent delegated a sub-task. Complete it independently "
    "using the available tools, then reply with a concise report of what you "
    "did and found. You cannot delegate to sub-subagents."
)

_DISPATCH_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": "Delegate a sub-task to a subagent that runs its own agent loop with all tools except subagent dispatch. Returns the subagent's final report.",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Sub-task description"}},
            "required": ["task"],
        },
    },
}
```

### `AgentSession.__init__` 新增参数
- `allow_subagent: bool = True`。
- 工具列表与 system prompt 按 `allow_subagent` 分叉：
  - `True`（主 agent）：`tools = TOOL_SCHEMAS + ([_USE_SKILL_SCHEMA] if skills)`；`_system_prompt = SYSTEM_PROMPT + skills_section`。
  - `False`（子 agent）：`tools = TOOL_SCHEMAS + ([_USE_SKILL_SCHEMA] if skills)`（**无派遣 schema**）；`_system_prompt = SYSTEM_PROMPT + skills_section + SUBAGENT_PROMPT_EXTRA`。
- `run_task` 内 `tools` 构建改为：`base = TOOL_SCHEMAS + ([_USE_SKILL_SCHEMA] if self.skills is not None else [])`；`if self.allow_subagent: base = base + [_DISPATCH_SUBAGENT_SCHEMA]`。

### `_run_tool` 分支
```python
if tc.name == "dispatch_subagent":
    if not self.allow_subagent:
        return ToolResult(ok=False, output="subagent dispatch is disabled for subagents")
    return self._dispatch_subagent(tc.arguments, on_delta=self._on_delta)
```
- 需将 `run_task` 的 `on_delta` 存为实例状态 `self._on_delta`（在 `run_task` 开头 `self._on_delta = on_delta`，`__init__` 中初始化为 `None`），供 `_run_tool` 内的派遣转发给子会话（流式透传）。

### import
- `agent.py` 顶部 `from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute` 追加 `truncate`。

### `_dispatch_subagent(arguments, on_delta) -> ToolResult`
```python
if not isinstance(arguments, dict) or not str(arguments.get("task", "")).strip():
    return ToolResult(ok=False, output="task is required")
task = str(arguments["task"]).strip()
try:
    sub = AgentSession(
        workdir=self.workdir,
        llm=self.llm,
        max_iterations=SUBAGENT_MAX_ITERATIONS,
        max_context_tokens=self.max_context_tokens,
        debug=self.debug,
        policy=self.policy,
        interact=self.interact,
        skills=self.skills,
        allow_subagent=False,
    )
    sub_result = sub.run_task(task, on_delta=on_delta)
except Exception as e:  # noqa: BLE001
    return ToolResult(ok=False, output=f"subagent dispatch failed: {type(e).__name__}: {e}")
if sub_result.final_text:
    out = sub_result.final_text
    if not sub_result.finished:
        out += f"\n\n[subagent status: {sub_result.reason}]"
else:
    out = f"(subagent returned no report; status: {sub_result.reason})"
out, truncated = truncate(out)
return ToolResult(ok=True, output=out, truncated=truncated)
```

### 子会话属性
- 继承：`workdir` / `llm` / `max_context_tokens` / `debug` / `policy` / `interact` / `skills`。
- 不继承（默认）：`store=None`、`workspace=None`、`session_id=None` → 子会话不持久化、不 touch workspace。
- `max_iterations=SUBAGENT_MAX_ITERATIONS(10)`。

## 6. 安全与继承

| 项 | 行为 |
|---|---|
| 权限继承 | 子会话继承 `policy` + `interact`；`--deny`/`--ask` 对子智能体工具调用同样生效 |
| skill | 子会话继承 `skills`，可用 `use_skill` |
| 会话持久化 | 子会话 store=None / workspace=None，不写 session / 不 touch workspace.json |
| 成本边界 | 子智能体 max_iterations=10；同步串行，单次派遣阻塞父级 |
| 上下文隔离 | 子会话全新 Conversation（仅子 prompt + 子任务），中间工具结果留在子上下文，只回传最终报告 |
| 阉割 | 双层强制，深度恒 1 |

## 7. 错误处理（全部回传模型，不崩溃循环）

| 场景 | 行为 |
|---|---|
| 缺/空白 `task` | `ok=false` "task is required" |
| 子会话 `run_task` 内部异常 | try/except 兜底 → `ok=false` "subagent dispatch failed: ..." |
| 子智能体正常完成 | `ok=true`，输出 final_text |
| 子智能体未完成 | `ok=true`，final_text + `[subagent status: <reason>]` |
| 子智能体空报告 | `(subagent returned no report; status: <reason>)` |
| 输出超长 | `truncate()` 8000 截断 |

## 8. 测试计划（全部离线，mock 模型）

**test_agent.py（扩展）**
1. 主 agent 派遣成功：FakeLLM 依次返回 dispatch_subagent 调用 → 子会话跑自己循环 → 子报告回填父级 tool 消息 → 父级终答含子报告。
2. 上下文隔离：子会话 `run_task` 时其 `conversation.messages` 仅含子 prompt + 子任务（无父级历史）。
3. 阉割第一层：子会话调用 `llm.chat` 的 `tools` 参数不含 `dispatch_subagent`。
4. 阉割第二层：`AgentSession(allow_subagent=False)` + FakeLLM 返回 dispatch_subagent → `_run_tool` 返回 `ok=false` "disabled"。
5. 缺 `task` → `ok=false` "task is required"。
6. 子智能体未完成：FakeLLM 驱动子会话连续失败达阈值 → 输出含 `[subagent status:`。
7. 权限继承：子会话 policy deny run_command → 子会话内 run_command 被拒（permission denied 出现在子会话 tool 结果中）。
8. 子会话不持久化：父级带 store 派遣后，子会话未创建 session 记录（对子会话断言 `store is None`，或父级派遣后 store 列表不变）。
9. 主 agent 工具列表含 `dispatch_subagent`（第一层反向断言）。

全部离线。冒烟（真实 API）：父级派"列出当前目录并总结每个文件用途"子任务，验证子智能体自主调用工具、回传报告、父级继续完成。

## 9. 文档同步

- `docs/architecture.md`：§3 agent.py 加 `allow_subagent` / `_dispatch_subagent` / `dispatch_subagent` 工具说明；工具计数 9 → 10。
- `docs/tools.md`：§1 工具数量 9 → 10；新增 §3.10 dispatch_subagent（参数/返回/阉割语义/权限继承）。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加（15）。
- `docs/development.md`：运行/测试说明（子智能体行为、权限继承、成本边界）。
- `code_agent/docs/superpowers/specs/2026-08-30-subagent-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-018（不入库，ADR-007）。

## 10. 开发顺序（小步推进，每步可验证）

1. `agent.py`：常量 + `allow_subagent` 参数 + 工具列表/system prompt 分叉（TDD：阉割第一/二层断言）
2. `agent.py`：`_run_tool` 分支 + `_dispatch_subagent`（TDD：派遣成功/上下文隔离/缺 task/未完成）
3. `agent.py`：权限继承 + 不持久化断言（TDD）
4. 文档同步 + ADR-018
5. 真实 API 冒烟（父派子完成子任务）
6. 全量回归 + 凭据复核 + 提交
