# 权限模型增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 工具执行前增加权限检查层——allow/ask/deny 三态规则 + 只读命令白名单 + doom_loop 重复检测。

**Architecture:** 新增 `permissions.py`（`Policy`：规则解析、三态求值 deny→ask→allow、只读白名单、doom_loop 连续计数）；`AgentSession` 可选 `policy`/`interact` 参数，`_run_tool` 前检查，deny 以 `ToolResult(ok=False)` 回传；`cli.py` 加 `--allow/--deny/--ask` 参数。

**Tech Stack:** Python 3.11+ 标准库（json/fnmatch/collections）。测试框架 pytest。

**Spec:** `docs/superpowers/specs/2026-08-29-permissions-design.md`

## Global Constraints

- Python 3.11+；零新依赖（stdlib json/fnmatch/collections）。
- 规则 `tool:pattern`；`run_command` 匹配文本 = `arguments["command"]`，其它工具 = `json.dumps(arguments, sort_keys=True, ensure_ascii=False)`；pattern 用 fnmatch。
- 三态求值：deny → ask → allow → 默认 allow；doom_loop 命中 → deny。
- 只读白名单仅 `run_command`；命令含 shell 运算符（`; | && || > < >> & `` ` `$(`）→ 不在白名单。
- doom_loop：连续相同 (tool, 匹配文本) ≥ 3 次 → deny（reason 含 doom_loop）。
- ask + interact=True：`input()` 询问 `[permission] ... [y/N]`，y→allow 其余→deny；interact=False → deny。
- 非法规则 → stderr 警告忽略。
- AgentSession 无 policy 时行为完全不变。
- 测试全部离线；`uv run pytest tests/ -q` 全绿后提交。
- 无凭据入库；提交保留完整历史，不 rebase。

---

### Task 1: Policy（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Create: `code_agent/code_agent/permissions.py`
- Create: `code_agent/tests/test_permissions.py`

**Interfaces:**
- Produces:
  - `parse_rule(rule: str) -> tuple[str, str] | None`。
  - `_is_readonly_command(command: str) -> bool`。
  - `class PermissionResult(decision: str, reason: str | None = None)`。
  - `class Policy(allow: list[str] | None, deny: list[str] | None, ask: list[str] | None)`
    - `check(tool: str, arguments: dict, interact: bool = False) -> PermissionResult`
    - `would_loop(tool: str, arguments: dict) -> bool`
  - Task 2 依赖 `Policy`；Task 3 依赖 `Policy` 构造。

- [ ] **Step 1: 写失败测试**

创建 `code_agent/tests/test_permissions.py`：

```python
from code_agent.permissions import Policy, parse_rule, _is_readonly_command


def test_parse_rule_valid():
    assert parse_rule("run_command:git status") == ("run_command", "git status")


def test_parse_rule_invalid():
    assert parse_rule("no-colon") is None
    assert parse_rule("tool:") is None


def test_check_default_allow():
    p = Policy()
    assert p.check("read_file", {"path": "a.txt"}).decision == "allow"


def test_check_deny_rule():
    p = Policy(deny=["run_command:pytest *"])
    assert p.check("run_command", {"command": "pytest tests/"}).decision == "deny"


def test_check_allow_rule():
    p = Policy(allow=["run_command:git *"])
    assert p.check("run_command", {"command": "git status"}).decision == "allow"


def test_deny_beats_allow():
    p = Policy(allow=["run_command:*"], deny=["run_command:pytest *"])
    assert p.check("run_command", {"command": "pytest x"}).decision == "deny"
    assert p.check("run_command", {"command": "ls"}).decision == "allow"


def test_ask_interactive_yes(monkeypatch):
    p = Policy(ask=["run_command:git push *"])
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert p.check("run_command", {"command": "git push"}, interact=True).decision == "allow"


def test_ask_interactive_no(monkeypatch):
    p = Policy(ask=["run_command:git push *"])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert p.check("run_command", {"command": "git push"}, interact=True).decision == "deny"


def test_ask_noninteractive_deny():
    p = Policy(ask=["run_command:git push *"])
    assert p.check("run_command", {"command": "git push"}, interact=False).decision == "deny"


def test_readonly_whitelist():
    p = Policy()
    assert p.check("run_command", {"command": "ls"}, interact=True).decision == "allow"
    assert p.check("run_command", {"command": "git status"}, interact=True).decision == "allow"
    assert _is_readonly_command("ls -la") is True
    assert _is_readonly_command("ls -la; rm -rf /") is False
    assert _is_readonly_command("echo hi > out.txt") is False


def test_would_loop():
    p = Policy()
    assert not p.would_loop("run_command", {"command": "echo hi"})
    p.check("run_command", {"command": "echo hi"})
    p.check("run_command", {"command": "echo hi"})
    assert p.would_loop("run_command", {"command": "echo hi"})
    assert p.check("run_command", {"command": "echo hi"}).decision == "deny"
    p.check("run_command", {"command": "echo bye"})
    assert p.check("run_command", {"command": "echo hi"}).decision == "allow"


def test_pattern_matches_command_text():
    p = Policy(deny=["run_command:git *"])
    assert p.check("run_command", {"command": "git status"}).decision == "deny"
    assert p.check("run_command", {"command": "git log"}).decision == "deny"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_permissions.py -v`
Expected: FAIL（ModuleNotFoundError: code_agent.permissions）

- [ ] **Step 3: 实现 permissions.py**

创建 `code_agent/code_agent/permissions.py`：

```python
"""Permission policy: allow/ask/deny rules, readonly whitelist, doom-loop detection."""
from __future__ import annotations

import fnmatch
import json
import sys
from collections import deque
from dataclasses import dataclass

DOOM_LOOP_LIMIT = 3

READONLY_PREFIXES = [
    "ls", "cat", "head", "tail", "grep", "rg", "pwd", "whoami",
    "echo", "date", "find", "wc", "file",
    "python3 -V", "python -V",
    "git status", "git log", "git diff", "git show", "git branch",
]

_SHELL_OPS = (";", "|", "&&", "||", ">", "<", ">>", "&", "`", "$(")


@dataclass
class PermissionResult:
    decision: str
    reason: str | None = None


def parse_rule(rule: str) -> tuple[str, str] | None:
    tool, _, pattern = rule.partition(":")
    tool = tool.strip()
    pattern = pattern.strip()
    if not tool or not pattern:
        return None
    return tool, pattern


def _match_text(tool: str, arguments: dict) -> str:
    if tool == "run_command":
        return str(arguments.get("command", ""))
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _is_readonly_command(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    for op in _SHELL_OPS:
        if op in stripped:
            return False
    lower = stripped.lower()
    return any(lower.startswith(prefix) for prefix in READONLY_PREFIXES)


class Policy:
    def __init__(
        self,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        ask: list[str] | None = None,
    ) -> None:
        self._allow = self._parse_rules(allow)
        self._deny = self._parse_rules(deny)
        self._ask = self._parse_rules(ask)
        self._calls: deque[tuple[str, str]] = deque(maxlen=DOOM_LOOP_LIMIT)

    @staticmethod
    def _parse_rules(rules: list[str] | None) -> list[tuple[str, str]]:
        parsed: list[tuple[str, str]] = []
        for rule in rules or []:
            x = parse_rule(rule)
            if x is None:
                print(f"[permission] warning: invalid rule ignored: {rule}", file=sys.stderr)
            else:
                parsed.append(x)
        return parsed

    def _matches(self, rules: list[tuple[str, str]], tool: str, text: str) -> bool:
        for rule_tool, pattern in rules:
            if rule_tool != tool:
                continue
            if fnmatch.fnmatch(text, pattern):
                return True
        return False

    def _same_streak(self, key: tuple[str, str]) -> int:
        streak = 0
        for k in reversed(self._calls):
            if k == key:
                streak += 1
            else:
                break
        return streak

    def would_loop(self, tool: str, arguments: dict) -> bool:
        key = (tool, _match_text(tool, arguments))
        return self._same_streak(key) >= DOOM_LOOP_LIMIT - 1

    def check(self, tool: str, arguments: dict, interact: bool = False) -> PermissionResult:
        text = _match_text(tool, arguments)
        key = (tool, text)
        if self.would_loop(tool, arguments):
            self._calls.append(key)
            return PermissionResult("deny", "doom_loop detected: repeated tool call")
        if self._matches(self._deny, tool, text):
            self._calls.append(key)
            return PermissionResult("deny", "denied by rule")
        if self._matches(self._ask, tool, text):
            if interact:
                prompt = f"[permission] {tool}({text[:60]!r}) allowed? [y/N] "
                decision = "allow" if input(prompt).strip().lower() == "y" else "deny"
            else:
                decision = "deny"
            self._calls.append(key)
            return PermissionResult(decision, "ask rule")
        if self._matches(self._allow, tool, text):
            self._calls.append(key)
            return PermissionResult("allow")
        if tool == "run_command" and _is_readonly_command(text):
            self._calls.append(key)
            return PermissionResult("allow", "readonly whitelist")
        self._calls.append(key)
        return PermissionResult("allow")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_permissions.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/permissions.py tests/test_permissions.py
git commit -m "feat: 权限模型 Policy（allow/ask/deny 三态 + 只读白名单 + doom_loop，ADR-014）"
```

---

### Task 2: AgentSession 集成（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Modify: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `Policy`（Task 1）。
- Produces: `AgentSession(..., policy: Policy | None = None, interact: bool = False)`；`_run_tool` 前检查，deny → `ToolResult(ok=False, output="permission denied: <reason>")`。Task 3 依赖 `policy`/`interact`。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_agent.py` 末尾追加：

```python
def test_agent_deny_rule_blocks_tool(workdir):
    from code_agent.permissions import Policy
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, policy=Policy(deny=["read_file:*"]))
    result = session.run_task("read the file")
    assert result.finished and result.final_text == "done"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert tool_msgs and "permission denied" in tool_msgs[0]["content"]


def test_agent_doom_loop_blocks_repeated_call(workdir):
    from code_agent.permissions import Policy
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    call = _read_call("c1", "a.txt")
    llm = FakeLLM([call, call, call])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=3, policy=Policy())
    result = session.run_task("loop")
    assert not result.finished and result.reason == "max_iterations"
    tool_msgs = [m for m in session.conversation.messages if m["role"] == "tool"]
    assert any("doom_loop" in m["content"] for m in tool_msgs)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v -k "deny or doom_loop"`
Expected: FAIL（TypeError: unexpected keyword argument 'policy'）

- [ ] **Step 3: 实现集成**

`code_agent/code_agent/agent.py`：

顶部 import 追加 `from code_agent.permissions import Policy`。

`__init__` 签名追加参数并在 `self.workspace = workspace` 之后赋值：

```python
        workspace: Workspace | None = None,
        policy: Policy | None = None,
        interact: bool = False,
    ) -> None:
```

```python
        self.workspace = workspace
        self.policy = policy
        self.interact = interact
```

`_run_tool` 改为：

```python
    def _run_tool(self, tc) -> ToolResult:
        if self.policy is not None:
            result = self.policy.check(tc.name, tc.arguments, interact=self.interact)
            if result.decision == "deny":
                reason = result.reason or tc.name
                return ToolResult(ok=False, output=f"permission denied: {reason}")
        try:
            return execute(tc.name, tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v`
Expected: 全绿（原 14 用例 + 新增 2）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: AgentSession 集成权限检查与 doom_loop（ADR-014）"
```

---

### Task 3: CLI 参数（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/cli.py`
- Modify: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `Policy`（Task 1）、`AgentSession.policy/interact`（Task 2）。
- Produces: `--allow/--deny/--ask`（action="append"）；`main` 构造 `Policy` 并传 `policy=policy, interact=args.interactive`。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_cli.py` 末尾追加：

```python
def test_main_policy_passed_and_rules_applied(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path),
               "--deny", "run_command:pytest *", "--allow", "read_file:*"])
    assert rc == 0
    policy = captured.get("policy")
    assert policy is not None
    assert captured.get("interact") is False
    assert policy.check("run_command", {"command": "pytest tests/"}).decision == "deny"
    assert policy.check("read_file", {"path": "a"}).decision == "allow"


def test_main_interactive_policy_interact(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def new_session(self):
            pass

        def load_session(self, sid):
            pass

    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    assert rc == 0
    assert captured.get("interact") is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v -k "policy"`
Expected: FAIL（`--deny` 无法识别 / policy 未传入）

- [ ] **Step 3: 实现 CLI**

`code_agent/code_agent/cli.py`：

顶部 import 追加 `from code_agent.permissions import Policy`。

`_build_parser` 追加（`--resume` 之后）：

```python
    parser.add_argument("--allow", action="append", default=[], metavar="TOOL:PATTERN", help="Allow rule (repeatable)")
    parser.add_argument("--deny", action="append", default=[], metavar="TOOL:PATTERN", help="Deny rule (repeatable)")
    parser.add_argument("--ask", action="append", default=[], metavar="TOOL:PATTERN", help="Ask rule (repeatable)")
```

`main` 中 `llm = _make_client(args)` 之后、`AgentSession` 构造之前插入：

```python
    policy = Policy(allow=args.allow, deny=args.deny, ask=args.ask)
```

`AgentSession(...)` 构造参数追加：

```python
            workspace=workspace,
            policy=policy,
            interact=args.interactive,
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v`
Expected: 全绿（原 15 用例 + 新增 2）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: CLI --allow/--deny/--ask 权限规则（ADR-014）"
```

---

### Task 4: 文档同步 + ADR-014（实现 → 验证 → 提交）

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（工作区根，仓库外，**不入库**，ADR-007）

**Interfaces:** 无。

- [ ] **Step 1: architecture.md**

模块总览表加一行（`workspace.py` 行之后）：

```markdown
| `permissions.py` | 权限模型：Policy（allow/ask/deny 三态、只读白名单、doom_loop） | 无（纯逻辑，标准库） |
```

§3 新增 `### permissions.py` 节（放 workspace.py 节之后）：

```markdown
### permissions.py
- `Policy(allow=None, deny=None, ask=None)` — 规则 `tool:pattern`（fnmatch）。
- `check(tool, arguments, interact=False) -> PermissionResult`：deny→ask→allow→默认 allow；run_command 应用只读白名单；doom_loop（连续相同调用 ≥3）→ deny。
- 交互询问：`[permission] ... [y/N]`，y→allow 其余→deny；非交互 ask→deny。
```

- [ ] **Step 2: design.md**

§6 "v0.1.0 已实现范围"追加：

```markdown
- [x] 权限模型（allow/ask/deny 三态 + 只读命令白名单 + doom_loop 重复检测，--allow/--deny/--ask）
```

§8 开发路线追加：

```markdown
11. [x] 迭代增强：权限模型（ADR-014，设计见 docs/superpowers/specs/2026-08-29-permissions-design.md）
```

- [ ] **Step 3: development.md**

§2 运行方式追加参数说明（`--resume` 说明之后）：

```markdown
- `--allow <tool:pattern>` / `--deny <tool:pattern>` / `--ask <tool:pattern>`：权限规则（可重复），如 `--deny "run_command:pytest *"`。
- 三态：deny 拒绝 → ask 询问（交互模式 y/N，一次性任务直接拒绝）→ allow 放行；内置只读命令白名单（ls/cat/git status 等）免询问。
- 连续相同工具调用达 3 次自动拒绝（doom_loop），防止模型重复卡死。
```

§3 测试目录说明更新用例数为 `133`（"当前 117 个用例" → "当前 133 个用例"），并加 `test_permissions.py` 说明行：

```markdown
  - `test_permissions.py`：规则解析/三态/只读白名单/doom_loop/交互询问。
```

- [ ] **Step 4: ADR-014**

`/home/kiki/workspace/code_agent_project/.agent/03-decisions.md` 的 `## 后续决策记录处` 之前追加：

```markdown
## ADR-014：权限模型增强（allow/ask/deny + 只读白名单 + doom_loop）
- **日期**：2026-08-29
- **状态**：已实施
- **背景**：工具执行无权限层，模型可自由执行任何命令；业界以三态 + 白名单作为第一道安全防线。
- **决策**：新增 `Policy`（规则 `tool:pattern`，deny→ask→allow→默认 allow）；内置只读命令白名单；doom_loop 连续相同调用 ≥3 拒绝；CLI `--allow/--deny/--ask`；ask 交互 y/N，非交互降级 deny。
- **理由**：成本低、安全收益高；非交互 deny 为安全默认。
- **影响**：新增 permissions.py；AgentSession 可选 policy/interact；受保护路径仍由 tools.py 强制。
```

- [ ] **Step 5: 验证与提交**

Run: `cd code_agent && uv run pytest tests/ -q`
Expected: 133 passed

Run: `cd code_agent && uv run python -m code_agent --help`
Expected: 含 `--allow` / `--deny` / `--ask`

Run: `cd code_agent && git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

```bash
cd code_agent
git add docs/architecture.md docs/design.md docs/development.md
git commit -m "docs: 同步权限模型文档并记录 ADR-014"
```

注意：ADR-014 更新的是工作区根 `.agent/03-decisions.md`（仓库外），本步 git add **不含** `.agent/`。

---

### Task 5: 真实 API 冒烟验证权限拒绝（冒烟 → 回归 → 提交）

**Files:** 无（如冒烟发现问题，修复对应文件）。

**Interfaces:** 无。

- [ ] **Step 1: 准备冒烟目录**

```bash
mkdir -p /tmp/code_agent_smoke5
cat > /tmp/code_agent_smoke5/demo.py <<'EOF'
def add(a, b):
    return a + b
EOF
```

- [ ] **Step 2: 运行冒烟（--deny 下 agent 调整）**

```bash
set -a; source /home/kiki/workspace/code_agent_project/code_agent/.env; set +a
cd /tmp/code_agent_smoke5
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent \
  --workdir /tmp/code_agent_smoke5 \
  --deny "run_command:pytest *" \
  --prompt "在 demo.py 后追加一行注释 'two'，然后尝试用 pytest 验证（会被拒绝），改用直接运行 python3 demo.py 或查看文件确认成功"
```

Expected: agent 尝试 `pytest` 被拒（conversation 中该 tool 结果含 "permission denied"），随后改用其它方式（如 `python3 -m py_compile demo.py` 或 read_file）完成任务，最终答复成功。

- [ ] **Step 3: 验证拒绝确实发生**

运行带 `--debug` 复跑一次，或检查保存的会话 tool 消息：

```bash
cd /tmp/code_agent_smoke5
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent \
  --workdir /tmp/code_agent_smoke5 --list-sessions
python3 - <<'EOF'
import glob, json
files = glob.glob('/tmp/code_agent_smoke5/.code_agent/sessions/*.jsonl')
if files:
    msgs = [json.loads(l) for l in open(files[-1]) if l.strip() and json.loads(l).get('role') == 'tool']
    denied = [m for m in msgs if 'permission denied' in m.get('content','')]
    print('denied_tool_messages=', len(denied))
EOF
```

Expected: `denied_tool_messages >= 1`（证明权限层生效）。

- [ ] **Step 4: 清理 + 全量回归 + 凭据复核 + 提交**

```bash
rm -rf /tmp/code_agent_smoke5
cd /home/kiki/workspace/code_agent_project/code_agent
uv run pytest tests/ -q
git grep -iE "sk-[a-zA-Z0-9]{10,}"
git status
```

Expected: 133 passed；凭据无命中。若冒烟无需代码修改，`git status` 干净则跳过提交，在报告中说明。

> 若无真实 API key 或网络不可用，改为在演示脚本中演示，并在报告标注"冒烟待真实验证"。
