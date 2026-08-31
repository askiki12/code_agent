# 迭代设计：工具层 Command + Registry 重构

> 日期：2026-09-01 ｜ 状态：已批准 ｜ 关联 ADR：ADR-025（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/tools.md` 为接口/使用权威源。

## 1. 背景与目标

当前工具系统的命令模式是**隐含**的：`tools.py` 用 `_HANDLERS` 字典（工具名 → 函数）+ `execute()` 分派；`agent.py` 的 `_run_tool` 对 `use_skill`/`dispatch_subagent` 用 `if` 特判，再对其它工具走 `execute()`。新增工具需要同时改 `TOOL_SCHEMAS`、`_HANDLERS`、可能还有 `_run_tool` 的特判——扩展路径不统一。

**目标**：把命令模式显式化——所有工具统一为 `Tool` Command 对象 + `ToolRegistry` 注册表；新增工具 = 实现一个 `Tool` 子类 + 注册，自动进入 schema 与分派。这是软件工程面试可强调的设计亮点（Command + Registry + Strategy 装饰权限）。

## 2. 迭代约束（用户决定）

- 保留顶层导出 `execute(name, args, workdir)` 与 `TOOL_SCHEMAS`（由默认 registry 派生），`test_tools.py` 大部分不破；**328 测试为安全网，重构后必须全绿**。
- `use_skill` / `dispatch_subagent` 是 session-bound 编排工具，统一进 registry，但**保留语义**：
  - 两者不经过 policy（`bypass_policy=True`，对应 tools.md "派遣动作本身不经过 policy 检查"）。
  - 阉割双层强制保留：`allow_subagent=False` 时 `dispatch_subagent` 的 schema 不可见（`visible=False`）+ 运行时仍返回 "subagent dispatch is disabled for subagents"。
  - `skills is None` 时 `use_skill` 的 schema 不可见（`visible=False`）。
- 无无关重构；只改本 spec 覆盖的文件。

## 3. 范围

**In scope**
- `code_agent/tools.py`：新增 `Tool` 基类（Command：name/description/parameters/required/bypass_policy/visible/schema()/validate()/execute()）+ `ToolRegistry`（register/get/schemas/execute，含 unknown-tool 与异常兜底）；9 个 stateless 工具各改成 `Tool` 子类；`_HANDLERS` 删除；顶层 `execute()`/`TOOL_SCHEMAS` 保留（由默认 registry 派生）。
- `code_agent/agent.py`：新增 `UseSkillTool` / `DispatchSubagentTool`（session-bound，bypass_policy=True）；`AgentSession.__init__` 构建 `self._registry`（BASE_TOOLS + 条件注册）；`run_task` 注入 `self._registry.schemas()`；`_run_tool` 统一为 registry 分派 + policy（bypass 除外）+ execute；`_USE_SKILL_SCHEMA`/`_DISPATCH_SUBAGENT_SCHEMA` 删除。
- 测试、文档同步、ADR-025。

**Out of scope（本期不做）**
- 其它模块的 Command 化（如 CLI 斜杠命令）。
- 并行工具调用、插件热加载。
- Builder 化 `AgentSession` 构造。

## 4. 设计

### 4.1 `tools.py`：Tool 基类 + ToolRegistry

```
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema properties
    required: list[str]
    bypass_policy: bool = False   # 编排工具置 True
    visible: bool = True          # False 时不注入 schema（阉割/条件能力）

    def schema(self) -> dict:     # 构建 OpenAI function schema（复用既有 _schema 逻辑）
    def validate(self, args: dict) -> str | None   # 缺必填/类型错误 → 错误消息；OK → None
    def execute(self, args: dict, workdir: str) -> ToolResult

class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None)
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool | None
    def schemas(self) -> list[dict]     # 仅 visible 工具
    def execute(self, name, args, workdir) -> ToolResult   # unknown-tool + 异常兜底
```

- 9 个 stateless 工具子类：`ReadFileTool`/`WriteFileTool`/`EditFileTool`/`ListDirTool`/`RunCommandTool`/`GlobTool`/`GrepTool`/`WebFetchTool`/`WebSearchTool`；`parameters`/`required` 从既有 `TOOL_SCHEMAS` 的 properties/required 迁移；`validate` 从既有参数守卫逻辑迁移（如缺 `path` → 错误消息）。
- 默认实例与顶层导出：
  ```
  _DEFAULT_REGISTRY = ToolRegistry([ReadFileTool(), WriteFileTool(), ...])
  TOOL_SCHEMAS = _DEFAULT_REGISTRY.schemas()
  def execute(name, args, workdir) -> ToolResult:
      return _DEFAULT_REGISTRY.execute(name, args, workdir)
  ```

### 4.2 `agent.py`：session-bound 工具 + registry 接线

```
class UseSkillTool(Tool):
    name="use_skill"; bypass_policy=True; parameters/required 从 _USE_SKILL_SCHEMA 迁移
    def __init__(self, session, *, visible): self._session=session; self.visible=visible
    def execute(self, args, workdir): return self._session._use_skill(args)

class DispatchSubagentTool(Tool):
    name="dispatch_subagent"; bypass_policy=True; 参数从 _DISPATCH_SUBAGENT_SCHEMA 迁移
    def __init__(self, session, *, visible): self._session=session; self.visible=visible
    def execute(self, args, workdir):
        if not self._session.allow_subagent:
            return ToolResult(ok=False, output="subagent dispatch is disabled for subagents")
        return self._session._dispatch_subagent(args, on_delta=self._session._on_delta)
```

`AgentSession.__init__` 尾部（skills/allow_subagent 已知后）：
```
tools = list(BASE_TOOLS)                          # from tools.py
tools.append(UseSkillTool(self, visible=self.skills is not None))
tools.append(DispatchSubagentTool(self, visible=self.allow_subagent))
self._registry = ToolRegistry(tools)
```

`run_task`：`tools = self._registry.schemas()`。

`_run_tool` 统一：
```
tool = self._registry.get(tc.name)
if tool is None:
    return ToolResult(ok=False, output=f"unknown tool: {tc.name}")
if not tool.bypass_policy and self.policy is not None:
    result = self.policy.check(tc.name, tc.arguments, interact=self.interact, ask=self.ask)
    if result.decision == "deny":
        return ToolResult(ok=False, output=f"permission denied: {result.reason or tc.name}")
try:
    return tool.execute(tc.arguments, self.workdir)
except Exception as e:
    return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")
```

## 5. 行为保留核对（必须逐项成立）

| 现状行为 | 重构后如何保留 |
|---|---|
| 9 个 stateless 工具 schema 与执行不变 | Tool 子类迁移 + 既有 test_tools 全绿 |
| `execute()` 顶层导出 + unknown-tool 消息 | `_DEFAULT_REGISTRY` 委托 + 消息不变 |
| dispatch_subagent/use_skill 不经过 policy | `bypass_policy=True` |
| allow_subagent=False：schema 不可见 + 运行时特定拒绝消息 | `visible=False` + `DispatchSubagentTool.execute` 内部判 allow_subagent |
| skills=None：use_skill schema 不可见 | `visible=False` |
| 工具调用前 `on_tool_start` 回调、调用后 `on_tool` 回调 | 在 `run_task` 循环层，不随重构变化 |
| 子会话继承 workdir/llm/policy/interact/skills、不持久化 | `_dispatch_subagent` 不变，只换 execute 包装 |
| 权限消息格式 `permission denied: <reason>` | 保留 |

## 6. 测试计划（全部离线）

- `test_tools.py`：
  1. 既有 `execute()` 用例全绿（行为不变）。
  2. `TOOL_SCHEMAS` 名称集合仍为 9。
  3. `ToolRegistry.register/get/schemas`（可见性过滤：visible=False 不进 schemas）。
  4. `ToolRegistry.execute` unknown-tool 消息 + 异常兜底。
  5. `Tool.validate`：缺必填 → 错误消息；合法 → None。
  6. 每个 Tool 子类 `schema()` 生成合法 OpenAI function schema（name/description/parameters.type=object）。
- `test_agent.py`：
  7. `run_task` 传给模型 tools 来自 `registry.schemas()`（mock LLM 捕获 tools 断言含 9+2 或按条件）。
  8. `_run_tool`：未知工具 → "unknown tool"；bypass_policy 工具（dispatch_subagent/use_skill）在 `--deny` 下仍执行（不 deny）；普通工具被 deny → "permission denied"。
  9. allow_subagent=False：tools 不含 dispatch_subagent schema；运行时调用仍返回 "subagent dispatch is disabled for subagents"。
  10. skills=None：tools 不含 use_skill schema。
- 全量回归：328+ 用例全绿；`uv run python -m code_agent --help` 正常；凭据 grep CLEAN。

## 7. 文档同步

- `architecture.md`：tools.py（Tool/ToolRegistry/9 子类，execute/TOOL_SCHEMAS 派生）、agent.py（UseSkillTool/DispatchSubagentTool/registry 接线/_run_tool 统一路径）。
- `tools.md`：schema 权威源改为 Tool 对象；注册表说明；`_HANDLERS` 不再存在。
- `design.md`：§6 勾选 + §8 追加（ADR-025）。
- `development.md`：测试目录说明（registry/Command 用例）。
- 工作区根 `.agent/03-decisions.md`：**ADR-025**（不入库）。

## 8. 开发顺序（小步推进，每步可验证）

1. `tools.py`：`Tool` 基类 + `ToolRegistry` + 9 个子类迁移 + 顶层导出派生 + `test_tools.py`（TDD）
2. `agent.py`：`UseSkillTool`/`DispatchSubagentTool` + registry 接线 + `_run_tool` 统一 + `test_agent.py`（TDD）
3. 文档同步 + ADR-025
4. 全量回归 + 凭据复核 + 提交

## 9. 后续步骤（本次迭代之外，重构完成后执行）

1. 更新 `human-in-the-loop-role.md`：追加本迭代为第 12 轮（设计模式显式化重构），写入决策表与人类裁决证据（方向选择、bypass_policy/visible 语义保留的裁决）。
2. 在工作区根（仓库外）撰写关键文档：a) 开发过程（AI+人类结对、敏捷、TDD、Human-in-the-Loop 实录）；b) 设计模式亮点（本文盘点 + Command+Registry 落地案例）。
