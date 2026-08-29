# 迭代设计：权限模型增强

> 日期：2026-08-29 ｜ 状态：已批准 ｜ 关联 ADR：ADR-014（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

当前工具执行无权限检查层：除受保护路径外，模型可自由执行任何工具与命令。业界（Claude Code/OpenCode）
以 allow/ask/deny 三态 + 只读白名单作为第一道安全防线；OpenCode 的 doom_loop 检测同一工具重复调用。
本迭代为工具执行前增加权限检查层，强化安全边界。

**目标**：实现 allow/ask/deny 三态规则 + 只读命令白名单 + doom_loop 重复检测；配置走 CLI 参数与内置默认，
非交互模式 ask 降级为 deny。

## 2. 范围

**In scope**
- 新增 `code_agent/permissions.py`：`Policy` 类（规则解析、三态求值、只读白名单、doom_loop 判定）。
- `AgentSession` 集成：`policy` 可选参数；`_run_tool` 前检查；deny 结果回传模型。
- CLI：`--allow <tool:pattern>` / `--deny <tool:pattern>` / `--ask <tool:pattern>`（可重复）。
- 交互模式 `interact=True`（ask 询问 y/N）；一次性 `--prompt` `interact=False`（ask → deny）。
- 测试、文档同步、ADR-014、真实 API 冒烟。

**Out of scope（本期不做）**
- 权限配置文件持久化（`.code_agent/` 下）、auto 模式（分类器审批）。
- 细粒度参数级规则（仅 tool:pattern，pattern 用 fnmatch 匹配序列化参数文本）。
- 网络白名单、沙箱（bwrap/Docker）。
- 会话级审批记忆（本次 ask 结果不持久化，每次询问）。

## 3. 权限模型

### 3.1 规则与求值

- 规则形式：`tool:pattern`，如 `run_command:git status`、`write_file:*`、`grep:*`。
- `pattern` 用 fnmatch 匹配**匹配文本**：
  - `run_command`：匹配文本 = `arguments["command"]` 字符串（如 `run_command:git *` 匹配 `git status`、`git log`）。
  - 其它工具：匹配文本 = `json.dumps(arguments, sort_keys=True, ensure_ascii=False)`（如 `write_file:*.env*` 匹配 path 含 `.env` 的写入）。
  - pattern 用 `*` 匹配该工具的任何调用（空 pattern 视为非法规则被忽略）；`tool` 部分精确匹配工具名。
- 三态求值顺序（deny → ask → allow）：
  1. **deny**：任一条 deny 规则命中 → 拒绝；受保护路径（tools.py 既有）强制拒绝。
  2. **ask**：任一条 ask 规则命中 → 有交互能力时询问，否则降级 deny。
  3. **allow**：任一条 allow 规则命中 或 只读白名单命中 → 放行。
  4. **默认 allow**：无任何规则命中 → 放行（保持现有行为兼容）。
- 优先级：deny > ask > allow（一旦命中更高优先级即定论，不继续）。

### 3.2 只读命令白名单

- 内置白名单（前缀匹配，命令首 token 或 `git <subcommand>`）：
  `ls, cat, head, tail, grep, rg, pwd, whoami, echo, date, find, wc, file, python3 -V, git status, git log, git diff, git show`
- 仅对 `run_command` 生效。
- 命令含 shell 运算符（`;` `|` `&&` `||` `>` `<` `>>` `&` `` ` `` `$(`）→ 不在白名单（保守，需走规则/默认）。
- 白名单匹配时直接 allow（不询问）。
- 注意：白名单是**预留快路径**——默认 allow 策略下它是惰性的（不改变任何判定），真正保护依赖 `--deny`/`--ask` 显式规则；显式规则优先于白名单。

### 3.3 doom_loop 重复检测

- 同一工具 + 相同序列化参数 连续调用达到 3 次 → 拒绝执行，返回提示（`doom_loop detected: repeated tool call ...`）。
- 计数器按调用序列维护；任何不同调用即重置连续计数。
- 拒绝后计数不清零（直到出现不同调用），防模型立即重试同调用。

## 4. Policy API（新增 `code_agent/permissions.py`）

```
class PermissionResult:
    decision: str   # "allow" | "ask" | "deny"
    reason: str | None

class Policy:
    def __init__(self, allow: list[str] | None = None,
                 deny: list[str] | None = None,
                 ask: list[str] | None = None) -> None
        # 规则列表，每条 "tool:pattern"；解析为 (tool, pattern)

    def check(self, tool: str, arguments: dict, interact: bool = False) -> PermissionResult
        # deny → ask(interact=True 时内部 input() 询问 y→allow 其余→deny；interact=False→deny)
        #      → allow/只读白名单 → 默认 allow
        # 仅 run_command 应用只读白名单；先做 would_loop 判定（命中→deny 标注 doom_loop）

    def would_loop(self, tool: str, arguments: dict) -> bool
        # doom_loop：连续相同调用 >= 3 次

    @staticmethod
    def readonly_prefixes() -> list[str]   # 内置只读白名单前缀

    @staticmethod
    def parse_rule(rule: str) -> tuple[str, str] | None
        # "tool:pattern" → (tool, pattern)；非法返回 None
```

- `check` 中若 `would_loop` 为真 → 直接 deny（reason 标注 doom_loop）。
- 参数序列化：`json.dumps(arguments, sort_keys=True, ensure_ascii=False)` 作为 pattern 匹配目标。
- 只读白名单判断独立实现（`_is_readonly_command(command) -> bool`）：拆分 shell 命令，检查运算符与前缀。

## 5. AgentSession 集成（agent.py）

- `__init__` 新增 `policy: Policy | None = None`；`self.policy = policy`、`self.interact: bool`（由 cli 传入，默认 False）。
- `_run_tool` 前检查：
  ```python
  if self.policy is not None:
      result = self.policy.check(tc.name, tc.arguments, interact=self.interact)
      if result.decision == "deny":
          return ToolResult(ok=False, output=f"permission denied: {result.reason or tc.name}")
  ```
- 现有 `execute` 的兜底捕获不变。
- 无 policy 时行为完全不变。
- doom_loop 状态（连续计数）由 `Policy.would_loop` 内部维护（Policy 实例保存最近调用 deque），agent 仅调用 check。

## 6. CLI（cli.py）

- 新参数：
  - `--allow <tool:pattern>`（action="append"，默认 []）
  - `--deny <tool:pattern>`（action="append"，默认 []）
  - `--ask <tool:pattern>`（action="append"，默认 []）
- `main` 构造 `Policy(allow=args.allow, deny=args.deny, ask=args.ask)`；一次性 `--prompt` 传 `interact=False`，交互模式传 `interact=True`。
- 非法规则（无 `:` 或无 tool）→ stderr 警告并忽略该条。
- 交互 ask 提示：`[permission] <tool>(<args 摘要>) allowed? [y/N] `；`y`/`Y` → allow，其余 → deny。

## 7. 安全

- 权限由 agent 强制，不由模型决定（模型无法绕开）。
- 受保护路径仍由 tools.py 强制（双保险）。
- deny 结果以 `ToolResult(ok=False)` 回传，agent 可调整。
- 非交互 ask → deny（安全默认）。

## 8. 错误处理

- 非法规则 → 警告忽略。
- ask 输入非 y/Y → 视为 deny。
- doom_loop 触发 → deny + 明确 reason。

## 9. 测试计划

**test_permissions.py（新增）**
1. parse_rule 正常与非法（无冒号/空 pattern）。
2. check 默认 allow（无规则）。
3. deny 规则命中 → deny。
4. allow 规则命中 → allow。
5. deny 优先级高于 allow（同工具两条规则）。
6. ask 规则：interact=True 且输入 y → allow；输入 n → deny；interact=False → deny。
7. 只读白名单：`ls`/`git status` → allow；含 `;`/`|` → 不走白名单（默认/规则）。
8. would_loop：3 次相同调用 → True；插入不同调用 → 重置。
9. pattern 匹配序列化参数（如 `run_command:git *` 匹配 git status 与 git log）。

**test_agent.py（扩展）**
10. 带 deny policy：对应工具返回 `ok=False` + "permission denied"，工具未实际执行。
11. doom_loop：相同调用 3 次 → 第 3 次起被拒（结果含 doom_loop）。
12. 无 policy 行为不变。

**test_cli.py（扩展）**
13. `--deny "run_command:pytest *"` 解析进 Policy 并传入 AgentSession（用 _FakeSession 断言 kwargs）。
14. 交互 ask 输入 mock（y/n 分支）。

全部离线。冒烟（真实 API）：`--deny "run_command:pytest *"` 下给任务，验证 agent 收到拒绝后调整（如改用其它方式）。

## 10. 文档同步

- `docs/architecture.md`：模块总览加 `permissions.py`；§3 接口约定（Policy API）。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加。
- `docs/development.md`：新 CLI 参数 `--allow/--deny/--ask` 与交互询问说明。
- `code_agent/docs/superpowers/specs/2026-08-29-permissions-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-014（不入库，ADR-007）。

## 11. 开发顺序（小步推进，每步可验证）

1. `permissions.py` + `test_permissions.py`（TDD）
2. `agent.py` 集成（含 interact/doom_loop）+ `test_agent.py`（TDD）
3. `cli.py` 参数 + `test_cli.py`（TDD）
4. 文档同步 + ADR-014
5. 真实 API 冒烟（--deny 下 agent 调整）
6. 全量回归 + 凭据复核 + 提交
