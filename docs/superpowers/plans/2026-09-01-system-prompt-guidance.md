# 系统提示词按能力注入（工具选用/技能编写/记忆/派遣指引）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化 `SYSTEM_PROMPT`，按可用能力条件注入四类指引（基础工具选用 / 技能使用与编写 / 记忆 / 子智能体派遣），并修复缺失 frontmatter 的 SKILL.md。

**Architecture:** 全部改动集中在 `code_agent/agent.py`：`SYSTEM_PROMPT` 常驻新增"Choosing tools"段；新增四个模块级指引常量 `DISPATCH_GUIDANCE` / `SKILL_GUIDANCE_USE` / `SKILL_GUIDANCE_CREATE` / `MEMORY_GUIDANCE`；`AgentSession.__init__` 按 `allow_subagent` / `skills` / `memory` 条件组装 `self._system_prompt`。测试加在 `tests/test_agent.py`（复用既有 `FakeLLM`/`_write_skill`/`workdir` fixture，全离线）。SKILL.md 修复在 `test_workspace/`（不入库，仅验证）。

**Tech Stack:** Python 3.11+ / pytest / 纯字符串拼接，无新依赖。

## Global Constraints

- 版本：Python 3.11+；运行依赖 requests/rich/textual 不动；不引入新依赖。
- 禁止 agent 框架/SDK、禁止服务端托管工具（红线）。
- 测试全离线（mock 模型），无需 API key。
- 保留既有 marker：`Available skills`、`You are a subagent`（`SUBAGENT_PROMPT_EXTRA` 文本不变）——兼容 `tests/test_agent.py:289,366`。
- 凭据不入库；提交信息可关联 ADR-027 / spec。
- 测试命令：`uv run pytest tests/test_agent.py -v`（全量：`uv run pytest tests/ -q`）。

---

### Task 1: SYSTEM_PROMPT 基础工具选用指引

**Files:**
- Modify: `code_agent/agent.py:19-39`（`SYSTEM_PROMPT` 常量，Rules 后追加段）
- Test: `tests/test_agent.py`（末尾追加测试）

**Interfaces:**
- Consumes: `FakeLLM`（`tests/test_agent.py:9-18`，`chat(messages, tools, on_delta)` 记录 `calls`）、`AgentSession`、`LLMResponse`、`workdir` fixture（`tests/conftest.py:4-7`）。
- Produces: 无（纯文本常量改动；后续 Task 不改这段）。

- [ ] **Step 1: 写失败测试**（`tests/test_agent.py` 末尾追加）

```python
def test_prompt_base_tool_guidance(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Choosing tools:" in system
    assert "Discover before reading" in system
    assert "edit_file" in system
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py::test_prompt_base_tool_guidance -v`
Expected: FAIL（`system` 中无 "Choosing tools:"，断言报错）。

- [ ] **Step 3: 实现**——修改 `code_agent/agent.py` 的 `SYSTEM_PROMPT`，在末尾（现有 Rule 6 之后、闭引号 `"""` 之前）追加一段：

```python
Choosing tools:
- Explore first: use list_dir/glob to locate files and grep to search contents — do not read whole files blindly.
- Read large files with read_file using offset/limit.
- Prefer edit_file (exact, unique substring) for surgical changes; use write_file for new files or whole-file rewrites.
- Verify your work with run_command (tests, compile, etc.).
- For external facts, use web_search to discover URLs, then web_fetch to read the full page.
```

（即 `SYSTEM_PROMPT` 中 Rule 6 之后新增独立段落，不修改已有 9 工具列表与 Rules 1-6。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py::test_prompt_base_tool_guidance -v`
Expected: PASS。

- [ ] **Step 5: 回归**：`uv run pytest tests/test_agent.py -v` 全绿。

- [ ] **Step 6: Commit**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: SYSTEM_PROMPT 新增基础工具选用指引段（关联 ADR-027）"
```

---

### Task 2: DISPATCH_GUIDANCE + MEMORY_GUIDANCE 条件注入

**Files:**
- Modify: `code_agent/agent.py:46-50`（`SUBAGENT_PROMPT_EXTRA` 之后新增两个常量）、`code_agent/agent.py:200-212`（`__init__` 组装）
- Test: `tests/test_agent.py`（末尾追加 4 个测试）

**Interfaces:**
- Consumes: Task 1 的 `SYSTEM_PROMPT`；`AgentSession` 构造参数 `allow_subagent`（默认 True）、`memory`（默认 False）。
- Produces: 常量 `DISPATCH_GUIDANCE: str`、`MEMORY_GUIDANCE: str`；组装顺序 `SYSTEM_PROMPT + (DISPATCH_GUIDANCE if allow_subagent) + skills_section + (MEMORY_GUIDANCE if memory) + (SUBAGENT_PROMPT_EXTRA if not allow_subagent)`。Task 3 依赖此组装点新增 `skills_section` 内容。

- [ ] **Step 1: 写失败测试**（`tests/test_agent.py` 末尾追加）

```python
def test_prompt_dispatch_guidance_present(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "When to delegate" in system
    assert "dispatch_subagent(task)" in system


def test_prompt_dispatch_guidance_absent_for_subagent(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, allow_subagent=False)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "When to delegate" not in system
    assert "You are a subagent" in system


def test_prompt_memory_guidance_present(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, memory=True)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "recall(query, top_k)" in system
    assert "remember(content, tags)" in system


def test_prompt_memory_guidance_absent(workdir):
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "recall(query, top_k)" not in system
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -k "dispatch_guidance or memory_guidance" -v`
Expected: 4 个测试 FAIL（提示词中无对应段）。

- [ ] **Step 3: 实现**——`code_agent/agent.py` 在 `SUBAGENT_PROMPT_EXTRA`（第 46-50 行）之后新增两个常量：

```python
DISPATCH_GUIDANCE = (
    "\n\nWhen to delegate: dispatch_subagent(task) runs an independent subagent "
    "that keeps all tools except subagent dispatch and returns a concise report. "
    "Use it for a sub-task that does not need shared context; do not use it for trivial work."
)

MEMORY_GUIDANCE = (
    "\n\nProject memory: recall(query, top_k) searches durable knowledge saved from "
    "prior sessions — query it before re-reading long documents. "
    "remember(content, tags) saves new facts/decisions/gotchas for future sessions. "
    "Relevant memories are auto-injected at task start and auto-summarized on success."
)
```

并把 `__init__` 的组装改为（第 208-212 行）：

```python
        self._system_prompt = (
            SYSTEM_PROMPT
            + (DISPATCH_GUIDANCE if allow_subagent else "")
            + skills_section
            + (MEMORY_GUIDANCE if memory else "")
            + (SUBAGENT_PROMPT_EXTRA if not allow_subagent else "")
        )
```

（本 Task 先保留现有 `skills_section` 构建逻辑不变，Task 3 再扩展。）

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py -k "dispatch_guidance or memory_guidance" -v`
Expected: 4 个测试 PASS。

- [ ] **Step 5: 回归**：`uv run pytest tests/test_agent.py -v` 全绿（含 `test_agent_subagent_tools_exclude_dispatch`，其断言 "You are a subagent" 仍成立）。

- [ ] **Step 6: Commit**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: 派遣/记忆指引按能力条件注入 system prompt（关联 ADR-027）"
```

---

### Task 3: SKILL_SECTION 扩展（使用 + 编写指南）

**Files:**
- Modify: `code_agent/agent.py`（新增 `SKILL_GUIDANCE_USE` / `SKILL_GUIDANCE_CREATE` 常量；扩展 `__init__` 的 `skills_section` 构建，约第 200-207 行）
- Test: `tests/test_agent.py`（复用既有 `_write_skill` helper，追加 3 个测试）

**Interfaces:**
- Consumes: Task 2 组装点；`skills: SkillRegistry | None`、`memory: bool`；既有 `_write_skill(root, name, desc, body)`（`tests/test_agent.py:272-277`）；`SkillRegistry(project_dir, user_dir)`（`code_agent/skills.py:42`）。
- Produces: 常量 `SKILL_GUIDANCE_USE: str`、`SKILL_GUIDANCE_CREATE: str`；`skills_section = "\n\nAvailable skills:\n<entries>" + SKILL_GUIDANCE_USE + (SKILL_GUIDANCE_CREATE if memory else "")`。

- [ ] **Step 1: 写失败测试**（`tests/test_agent.py` 末尾追加）

```python
def test_prompt_skill_use_guidance(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Available skills" in system
    assert "use_skill(name)" in system


def test_prompt_skill_create_guidance_when_memory(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg, memory=True)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Authoring skills" in system
    assert "create_skill(name, description, content)" in system
    assert ".code_agent" in system


def test_prompt_skill_create_guidance_absent_without_memory(workdir, tmp_path):
    from code_agent.skills import SkillRegistry
    proj = str(tmp_path / "proj")
    _write_skill(proj, "code-review", "review code", "step1")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[])])
    session = AgentSession(workdir=workdir, llm=llm, max_iterations=2, skills=reg)
    session.run_task("hi")
    system = llm.calls[0][0]["content"]
    assert "Authoring skills" not in system
```

注：`memory=True` 且任务成功结束时 `_auto_memorize` 会再调一次 `llm.chat`（`FakeLLM` 响应用尽抛 `IndexError`），该方法整体在 `try/except Exception` 内（`agent.py:369`），测试不受影响。

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -k "skill_use_guidance or skill_create_guidance" -v`
Expected: 3 个测试 FAIL（`use_skill(name)`/`Authoring skills` 不在提示词中）。

- [ ] **Step 3: 实现**——`code_agent/agent.py` 新增两个常量（放在 `MEMORY_GUIDANCE` 之后）：

```python
SKILL_GUIDANCE_USE = (
    "\n\nSkills: use_skill(name) loads a skill's SKILL.md instructions into context — "
    "call it when the task matches an available skill, then follow its steps."
)

SKILL_GUIDANCE_CREATE = (
    "\n\nAuthoring skills: a skill is a SKILL.md file at "
    "<workdir>/.code_agent/skills/<name>/SKILL.md (project-level) or "
    "~/.code_agent/skills/<name>/SKILL.md (user-level; project wins on name conflict). "
    "Format: frontmatter then a markdown body:\n"
    "---\nname: <letters, digits, - or _>\ndescription: one-line 'when to use this'\n---\n"
    "<reusable steps, gotchas, exact commands>\n"
    "Create it with create_skill(name, description, content) — it writes the file for you; "
    ".code_agent is a protected path, so never write_file/edit_file a skill by hand. "
    "The description shows in the skill list and picker, so make it matchable. "
    "Write a skill after finishing a reusable, non-trivial workflow."
)
```

并把 `__init__` 中 `skills_section` 构建改为（第 200-207 行）：

```python
        skills_section = ""
        if skills is not None:
            entries = "\n".join(f"- {s.name}: {s.description}" for s in skills.scan())
            if entries:
                skills_section = (
                    "\n\nAvailable skills:\n" + entries
                    + SKILL_GUIDANCE_USE
                    + (SKILL_GUIDANCE_CREATE if memory else "")
                )
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py -k "skill_use_guidance or skill_create_guidance" -v`
Expected: 3 个测试 PASS。

- [ ] **Step 5: 回归**：`uv run pytest tests/test_agent.py -v` 全绿（含既有 `test_agent_with_skills_injects_prompt`：skills 无 memory，`Available skills`/`code-review` 仍在、编写段不注入）。

- [ ] **Step 6: Commit**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: skill 使用/编写指南按能力注入（skills+memory 条件，关联 ADR-027）"
```

---

### Task 4: 修复 SKILL.md frontmatter 并验证

**Files:**
- Modify: `/home/kiki/workspace/code_agent_project/test_workspace/.code_agent/skills/toutiao_top3_fetch/SKILL.md`（不入库，仅本地验证）

**Interfaces:**
- Consumes: `SkillRegistry.scan()` / `load(name)`（`code_agent/skills.py:76-97`），frontmatter 规则 `_parse_frontmatter`（`skills.py:17-35`，要求 `---` 开头 + `name`/`description`）。
- Produces: 无（本地文件修复）。

- [ ] **Step 1: 在文件头部插入 frontmatter**

把 `test_workspace/.code_agent/skills/toutiao_top3_fetch/SKILL.md` 第一行前插入：

```markdown
---
name: toutiao_top3_fetch
description: 抓取今日头条热榜 Top3 并整理为 Markdown 文档
---

```

正文（`# 抓取今日头条热榜 Top3 并整理为文档` 起）保持不变。

- [ ] **Step 2: 验证可被扫描识别**

Run: `cd /home/kiki/workspace/code_agent_project/code_agent && uv run python -c "from code_agent.skills import SkillRegistry; r=SkillRegistry('/home/kiki/workspace/code_agent_project/test_workspace'); s=r.scan(); print([ (x.name, x.description) for x in s ]); print(bool(r.load('toutiao_top3_fetch')))"`
Expected: `[('toutiao_top3_fetch', '抓取今日头条热榜 Top3 并整理为 Markdown 文档')]` 且 `True`；无 `[skills] warning: invalid SKILL.md skipped` 输出。

- [ ] **Step 3: 本地冒烟（真实链路，用户确认后可选）**

Run: `uv run python -m code_agent --workdir ../test_workspace --list-sessions`
Expected: 正常列出会话；交互（TTY）下 Ctrl+S 应列出 `toutiao_top3_fetch`。
（真实 API 冒烟是否执行由用户决定，本步不强制。）

- [ ] **Step 4: 无提交**（`test_workspace/` 在仓库外）

---

### Task 5: 文档同步 + ADR-027

**Files:**
- Modify: `code_agent/docs/architecture.md`（agent.py 小节）、`code_agent/docs/design.md`（特性清单 + 开发路线）、`/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（追加 ADR-027）

**Interfaces:**
- Consumes: 已完成的行为变更（Task 1-3）。
- Produces: 文档记录，供后续回溯。

- [ ] **Step 1: 更新 `code_agent/docs/architecture.md`**

在 `### agent.py` 小节（约第 129 行起）的 `AgentSession` 说明中补充：`self._system_prompt` 按能力条件组装——`SYSTEM_PROMPT`（含基础工具选用指引 Choosing tools 段）`+ DISPATCH_GUIDANCE(allow_subagent) + skills_section(skills 非空：Available skills 列表 + SKILL_GUIDANCE_USE + SKILL_GUIDANCE_CREATE(memory)) + MEMORY_GUIDANCE(memory) + SUBAGENT_PROMPT_EXTRA(非 subagent 反向)`。

- [ ] **Step 2: 更新 `code_agent/docs/design.md`**

在 §6 功能范围特性列表追加一行：`- [x] 系统提示词按能力注入：基础工具选用指引 + skill 使用/编写指南 + 记忆/派遣指引（条件注入，ADR-027）`；并在 §8 开发路线追加条目 25。

- [ ] **Step 3: 追加 ADR-027**（`/home/kiki/workspace/code_agent_project/.agent/03-decisions.md` "后续决策记录处" 之前）

```markdown
## ADR-027：系统提示词按能力注入（工具选用/技能编写/记忆/派遣指引）
- **日期**：2026-09-01
- **状态**：已实施
- **背景**：SYSTEM_PROMPT 只列 9 个基础工具 + 通用规则，模型虽经 tool schema 可见 dispatch_subagent/use_skill/remember/recall/create_skill，但提示词无"何时用哪个工具、技能怎么写/写哪/长什么样"的规范引导；真实使用中发现旧会话把纯 markdown 的 SKILL.md 搬到技能目录（缺 frontmatter）被静默跳过，Ctrl+S 显示无可用技能。
- **决策**：按能力条件注入四类指引——基础工具选用（Choosing tools 段）常驻；SKILL_GUIDANCE_USE 随技能库注入、SKILL_GUIDANCE_CREATE 仅 memory=True（create_skill 可用）注入；MEMORY_GUIDANCE 仅 memory=True 注入；DISPATCH_GUIDANCE 仅 allow_subagent=True 注入。保留 Available skills / You are a subagent 标记。SKILL.md 规范写入提示词：frontmatter 格式、项目/用户级位置、必须用 create_skill（.code_agent 受保护禁 write_file）。
- **理由**：提示词与模型实际可用工具一致（子智能体不被提示不能用的能力），token 更省；把"SKILL.md 必须带 frontmatter、创建走 create_skill"的约束前置给模型，从源头避免无效技能。
- **影响**：agent.py 提示词组装 + 4 项新测试；docs/architecture.md、docs/design.md 同步。
```

- [ ] **Step 4: 回归 + 凭据复核**

Run: `cd /home/kiki/workspace/code_agent_project/code_agent && uv run pytest tests/ -q` → 全绿（370 + 8 新增）。
Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"` → 无命中。

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md docs/design.md
git commit -m "docs: 同步系统提示词按能力注入文档并记录 ADR-027"
```

---

## Self-Review 记录

- **Spec 覆盖**：§4.1→Task 1；§4.2→Task 3；§4.3/§4.4→Task 2；§4.5 组装顺序→Task 2+3；§4.6→Task 4；§5 测试→Task 1/2/3；§6 文档+ADR→Task 5；§7 验收→Task 4/5 的 Step 4。无缺口。
- **Placeholder 扫描**：无 TBD/TODO；所有实现步骤含完整代码。
- **类型一致性**：常量名 `DISPATCH_GUIDANCE`/`SKILL_GUIDANCE_USE`/`SKILL_GUIDANCE_CREATE`/`MEMORY_GUIDANCE` 在 Task 2/3 中一致；`_write_skill`、`FakeLLM`、`AgentSession` 签名沿用既有。
