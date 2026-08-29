# 工作区一等公民 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `--workdir` 成为一等公民——持久化工作区身份与最近会话，交互启动展示项目概况并提示续接。

**Architecture:** 新增 `workspace.py`（`Workspace`，幂等读写 `<workdir>/.code_agent/workspace.json`，原子写、损坏容错）；`AgentSession` 可选 `workspace` 参数，`run_task` 结束后 `touch_session`；`cli.py` 任意模式初始化，交互模式启动展示工作区行 + Tip，`--prompt` 静默。

**Tech Stack:** Python 3.11+ 标准库（json/os/hashlib/datetime）。测试框架 pytest。

**Spec:** `docs/superpowers/specs/2026-08-29-workspace-design.md`

## Global Constraints

- Python 3.11+；零新依赖（stdlib json/os/hashlib/datetime）。
- 文件：`<workdir>/.code_agent/workspace.json`；字段 `{id, name, path, created_at, updated_at, last_session_id?}`。
- `id = sha1(os.path.realpath(workdir))[:12]`（稳定）；`name = basename(realpath)`；`path = realpath`。
- 时间戳 `datetime.now().isoformat(timespec="microseconds")`（沿用已 ratify 的微秒约定）。
- 幂等初始化：重复构造不覆盖 created_at；json 损坏/非法 → stderr 警告 + 重建。
- 原子写（tmp + `os.replace`）。
- `session_count` 不持久化，展示时由 CLI 经 `store.list_sessions()` 实时获取。
- `touch_session(session_id)`：更新 last_session_id + updated_at；写失败 OSError 由调用方捕获警告（不崩溃）。
- AgentSession 无 workspace 时行为完全不变。
- `--prompt` 一次性不打印工作区行；`--list-sessions` 不打印工作区行。
- `.code_agent/` 已受保护（工具层禁读写）；gitignore 已忽略。
- 测试全部离线；`uv run pytest tests/ -q` 全绿后提交。
- 无凭据入库；提交保留完整历史，不 rebase。

---

### Task 1: Workspace（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Create: `code_agent/code_agent/workspace.py`
- Create: `code_agent/tests/test_workspace.py`

**Interfaces:**
- Produces:
  - `_make_workspace_id(workdir: str) -> str`：`sha1(realpath)[:12]`。
  - `class Workspace(workdir: str)`
    - `.data -> dict`（副本）、`.id -> str`、`.name -> str`、`.last_session_id -> str | None`。
    - `touch_session(session_id: str) -> None`：更新 last_session_id + updated_at，原子写。
    - `display() -> str`：`"Workspace: <name> (<id>)"`。
  - Task 2 依赖 `Workspace`；Task 3 依赖 `Workspace` 与 `display`。

- [ ] **Step 1: 写失败测试**

创建 `code_agent/tests/test_workspace.py`：

```python
import os

from code_agent.workspace import Workspace, _make_workspace_id


def test_init_creates_workspace_file(tmp_path):
    w = Workspace(str(tmp_path))
    assert os.path.isfile(os.path.join(str(tmp_path), ".code_agent", "workspace.json"))
    assert w.name == os.path.basename(str(tmp_path))


def test_init_idempotent_preserves_created_at(tmp_path):
    w1 = Workspace(str(tmp_path))
    w2 = Workspace(str(tmp_path))
    assert w1.data["created_at"] == w2.data["created_at"]
    assert w1.id == w2.id


def test_id_is_stable_hash(tmp_path):
    w1 = Workspace(str(tmp_path))
    w2 = Workspace(str(tmp_path))
    assert w1.id == w2.id == _make_workspace_id(str(tmp_path))
    assert len(w1.id) == 12


def test_touch_session_updates_last_and_updated_at(tmp_path):
    w = Workspace(str(tmp_path))
    before = w.data["updated_at"]
    w.touch_session("code_agent-1")
    assert w.last_session_id == "code_agent-1"
    assert w.data["updated_at"] != before


def test_corrupt_json_rebuilds(tmp_path, capsys):
    p = os.path.join(str(tmp_path), ".code_agent", "workspace.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("not-json")
    w = Workspace(str(tmp_path))
    assert "warning" in capsys.readouterr().err
    assert w.data["id"] == _make_workspace_id(str(tmp_path))


def test_display_contains_name_and_id(tmp_path):
    w = Workspace(str(tmp_path))
    assert w.display() == f"Workspace: {w.name} ({w.id})"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_workspace.py -v`
Expected: FAIL（ModuleNotFoundError: code_agent.workspace）

- [ ] **Step 3: 实现 workspace.py**

创建 `code_agent/code_agent/workspace.py`：

```python
"""Workspace identity and metadata (first-class working directory)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime


def _make_workspace_id(workdir: str) -> str:
    real = os.path.realpath(workdir)
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


class Workspace:
    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self._dir = os.path.join(workdir, ".code_agent")
        self._path = os.path.join(self._dir, "workspace.json")
        self._data = self._load_or_init()

    def _load_or_init(self) -> dict:
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if self._valid(data):
                    return data
                print("[workspace] warning: invalid workspace.json, rebuilding", file=sys.stderr)
            except (OSError, json.JSONDecodeError) as e:
                print(f"[workspace] warning: failed to read workspace.json ({e}), rebuilding", file=sys.stderr)
        return self._create()

    @staticmethod
    def _valid(data) -> bool:
        return isinstance(data, dict) and all(k in data for k in ("id", "name", "path"))

    def _create(self) -> dict:
        os.makedirs(self._dir, exist_ok=True)
        real = os.path.realpath(self.workdir)
        now = _now()
        data = {
            "id": _make_workspace_id(self.workdir),
            "name": os.path.basename(real) or real,
            "path": real,
            "created_at": now,
            "updated_at": now,
        }
        self._write(data)
        return data

    def _write(self, data: dict) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self._path)

    @property
    def data(self) -> dict:
        return dict(self._data)

    @property
    def id(self) -> str:
        return self._data["id"]

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def last_session_id(self) -> str | None:
        return self._data.get("last_session_id")

    def touch_session(self, session_id: str) -> None:
        self._data["last_session_id"] = session_id
        self._data["updated_at"] = _now()
        self._write(self._data)

    def display(self) -> str:
        return f"Workspace: {self.name} ({self.id})"
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_workspace.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/workspace.py tests/test_workspace.py
git commit -m "feat: 工作区一等公民 Workspace（幂等元数据，原子写，ADR-013）"
```

---

### Task 2: AgentSession 集成（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Modify: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `Workspace`（Task 1）。
- Produces: `AgentSession(..., workspace: Workspace | None = None)`；`run_task` finally 保存块中在 `store.save` 成功后调用 `self.workspace.touch_session(self.session_id)`（OSError 一并捕获警告）。Task 3 依赖 `workspace` 参数。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_agent.py` 末尾追加：

```python
def test_agent_with_workspace_touches_session(workdir, tmp_path):
    from code_agent.session import SessionStore
    from code_agent.workspace import Workspace
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    workspace = Workspace(str(tmp_path))
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5, store=store, workspace=workspace)
    result = session.run_task("read the file")
    assert result.finished
    assert workspace.last_session_id == session.session_id


def test_agent_without_workspace_unchanged(workdir):
    Path(workdir, "a.txt").write_text("hello", encoding="utf-8")
    llm = FakeLLM([
        _read_call("c1", "a.txt"),
        LLMResponse(content="done", tool_calls=[]),
    ])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=5)
    result = session.run_task("read the file")
    assert result.finished and result.final_text == "done"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v -k "workspace"`
Expected: `test_agent_with_workspace_touches_session` FAIL（TypeError: unexpected keyword argument 'workspace'）；`test_agent_without_workspace_unchanged` 通过

- [ ] **Step 3: 实现集成**

`code_agent/code_agent/agent.py`：

顶部 import 追加 `from code_agent.workspace import Workspace`。

`__init__` 签名追加参数并赋值：

```python
        store: SessionStore | None = None,
        session_id: str | None = None,
        resume: bool = False,
        workspace: Workspace | None = None,
    ) -> None:
```

在 `self.store = store` 之后追加 `self.workspace = workspace`。

`run_task` 的 `finally:` 保存块改为：

```python
        finally:
            if self.store is not None:
                title = self._title()
                try:
                    if self.session_id is None:
                        self.session_id = self.store.create(title)
                    self.store.save(self.session_id, self.conversation.messages, title=title)
                    if self.workspace is not None:
                        self.workspace.touch_session(self.session_id)
                except OSError as e:
                    print(f"[agent] warning: failed to save session: {e}", file=sys.stderr)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_agent.py -v`
Expected: 全绿（原 12 用例 + 新增 2）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: AgentSession 集成工作区 touch_session（ADR-013）"
```

---

### Task 3: CLI 展示（失败测试 → 实现 → 全绿 → 提交）

**Files:**
- Modify: `code_agent/code_agent/cli.py`
- Modify: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `Workspace`（Task 1）、`AgentSession.workspace`（Task 2）。
- Produces: `main` 中 `try: workspace = Workspace(workdir) except OSError: workspace = None`；交互模式启动打印工作区行（含实时 sessions 计数与 last），有 last 且会话存在时打印 Tip；`--prompt`/`--list-sessions` 不打印工作区行。

- [ ] **Step 1: 写失败测试**

在 `code_agent/tests/test_cli.py` 末尾追加：

```python
def test_main_interactive_shows_workspace(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    from code_agent.workspace import Workspace
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("existing")
    store.save(sid, [{"role": "user", "content": "hi"}])
    Workspace(str(tmp_path)).touch_session(sid)
    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Workspace:" in out and "sessions: 1" in out and "last:" in out
    assert "Tip: /resume" in out and sid in out
    assert rc == 0


def test_main_oneshot_does_not_show_workspace(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--prompt", "do it", "--workdir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Workspace:" not in out
```

- [ ] **Step 2: 运行确认失败**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v -k "workspace"`
Expected: 2 个新用例 FAIL（输出不含 Workspace: / Tip）

- [ ] **Step 3: 实现 CLI 展示**

`code_agent/code_agent/cli.py`：

顶部 import 追加 `from code_agent.workspace import Workspace`。

`main` 中 `_load_dotenv()` 之后、`store` 构造之前插入：

```python
    try:
        workspace = Workspace(workdir)
    except OSError:
        workspace = None
```

`AgentSession(...)` 构造参数末尾追加 `workspace=workspace`。

`main` 的交互模式横幅段替换为：

```python
    if workspace is not None:
        sessions = store.list_sessions()
        line = workspace.display() + f" | sessions: {len(sessions)}"
        last = workspace.last_session_id
        if last and any(s["id"] == last for s in sessions):
            line += f" | last: {last}"
            print(line)
            print(f"Tip: /resume {last} 续接上次会话")
        else:
            print(line)
    print("Interactive mode. Type 'exit', 'quit' or '/exit' to leave. Commands: /new /list /resume <id>")
```

（`--prompt` 分支在交互展示之前，故一次性任务不打印工作区行；`--list-sessions` 分支在 workspace 初始化之后但自身直接返回，不打印工作区行。）

- [ ] **Step 4: 运行确认通过**

Run: `cd code_agent && uv run pytest tests/test_cli.py -v`
Expected: 全绿（原 11 用例 + 新增 2）

- [ ] **Step 5: 提交**

```bash
cd code_agent
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: CLI 交互启动展示工作区概况与续接提示（ADR-013）"
```

---

### Task 4: 文档同步 + ADR-013（实现 → 验证 → 提交）

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（工作区根，仓库外，**不入库**，ADR-007）

**Interfaces:** 无。

- [ ] **Step 1: architecture.md**

模块总览表加一行（`session.py` 行之后）：

```markdown
| `workspace.py` | 工作区身份与元数据：Workspace（workspace.json 幂等读写/触摸/展示） | 无（纯逻辑，标准库） |
```

§3 新增 `### workspace.py` 节（放 session.py 节之后）：

```markdown
### workspace.py
- `Workspace(workdir)` — 读取/初始化 `<workdir>/.code_agent/workspace.json`（id = sha1(realpath)[:12]，name = basename）。
- `touch_session(session_id)`：更新 last_session_id + updated_at（原子写）。
- `display() -> str`："Workspace: <name> (<id>)"；实时统计由 CLI 拼接。
```

- [ ] **Step 2: design.md**

§6 "v0.1.0 已实现范围"追加：

```markdown
- [x] 工作区一等公民（workspace.json 元数据，交互启动展示概况与上次会话续接提示）
```

§8 开发路线追加：

```markdown
10. [x] 迭代增强：工作区一等公民（ADR-013，设计见 docs/superpowers/specs/2026-08-29-workspace-design.md）
```

- [ ] **Step 3: development.md**

§2 运行方式追加说明（交互模式段之后）：

```markdown
- 交互模式启动会展示工作区概况：`Workspace: <name> (<id>) | sessions: <n> | last: <last_session_id>`，并提示 `Tip: /resume <last_session_id>` 续接上次会话。
- 工作区元数据存于 `<workdir>/.code_agent/workspace.json`（自动维护，勿手动编辑）。
```

§3 测试目录说明更新用例数为 `115`（"当前 105 个用例" → "当前 115 个用例"），并加 `test_workspace.py` 说明行：

```markdown
  - `test_workspace.py`：工作区初始化/幂等/损坏容错/touch_session。
```

- [ ] **Step 4: ADR-013**

`/home/kiki/workspace/code_agent_project/.agent/03-decisions.md` 的 `## 后续决策记录处` 之前追加：

```markdown
## ADR-013：工作区一等公民
- **日期**：2026-08-29
- **状态**：已实施
- **背景**：`--workdir` 仅作执行目录，无项目身份与跨启动状态；业界（Claude Code/OpenCode/Codex）均以当前目录为工作区并展示概况。
- **决策**：新增 `Workspace`，持久化 `<workdir>/.code_agent/workspace.json`（id=sha1(realpath)[:12]、name、path、时间戳、last_session_id）；交互启动展示概况与续接提示；`--prompt` 静默。
- **理由**：轻量元数据为后续项目级配置（权限规则等）打地基；id 稳定可跨启动识别同一目录。
- **影响**：新增 workspace.py；AgentSession 可选 workspace 参数；workspace.json 位于已受保护的 `.code_agent/`。
```

- [ ] **Step 5: 验证与提交**

Run: `cd code_agent && uv run pytest tests/ -q`
Expected: 115 passed

Run: `cd code_agent && uv run python -m code_agent --help`
Expected: 正常输出（不变）

Run: `cd code_agent && git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

```bash
cd code_agent
git add docs/architecture.md docs/design.md docs/development.md
git commit -m "docs: 同步工作区文档并记录 ADR-013"
```

注意：ADR-013 更新的是工作区根 `.agent/03-decisions.md`（仓库外），本步 git add **不含** `.agent/`。

---

### Task 5: 真实 API 冒烟验证工作区稳定性（冒烟 → 回归 → 提交）

**Files:** 无（如冒烟发现问题，修复对应文件）。

**Interfaces:** 无。

- [ ] **Step 1: 准备冒烟目录**

```bash
mkdir -p /tmp/code_agent_smoke4
cat > /tmp/code_agent_smoke4/demo.py <<'EOF'
def add(a, b):
    return a + b
EOF
```

- [ ] **Step 2: 首次启动建会话**

```bash
set -a; source /home/kiki/workspace/code_agent_project/code_agent/.env; set +a
cd /tmp/code_agent_smoke4
uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent \
  --workdir /tmp/code_agent_smoke4 --prompt "读 demo.py，一句话说明 add 做什么"
```

Expected: 正常答复。随后检查工作区文件：

```bash
cat /tmp/code_agent_smoke4/.code_agent/workspace.json
```

Expected: 含 id（12 hex）、name=demo 目录名、last_session_id 已填入。

- [ ] **Step 3: 第二次启动验证 id 稳定 + 交互展示**

```bash
WID1=$(python3 -c "import json;print(json.load(open('/tmp/code_agent_smoke4/.code_agent/workspace.json'))['id'])")
echo "$WID1" | uv run --project /home/kiki/workspace/code_agent_project/code_agent python -m code_agent --workdir /tmp/code_agent_smoke4 --list-sessions 2>/dev/null >/dev/null
cat /tmp/code_agent_smoke4/.code_agent/workspace.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('id_stable=', d['id']=='$WID1')"
```

Expected: `id_stable= True`（同一目录跨启动 id 不变）。

- [ ] **Step 4: 清理 + 全量回归 + 凭据复核 + 提交**

```bash
rm -rf /tmp/code_agent_smoke4
cd /home/kiki/workspace/code_agent_project/code_agent
uv run pytest tests/ -q
git grep -iE "sk-[a-zA-Z0-9]{10,}"
git status
```

Expected: 115 passed；凭据无命中。若冒烟无需代码修改，`git status` 干净则跳过提交，在报告中说明。

> 若无真实 API key 或网络不可用，改为在演示脚本中演示，并在报告标注"冒烟待真实验证"。
