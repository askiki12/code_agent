# 迭代设计：系统提示词按能力注入（工具选用指引 + 技能编写指南 + 记忆/派遣指引）

> 日期：2026-09-01 ｜ 状态：已批准 ｜ 关联 ADR：ADR-027（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 为接口权威源。

## 1. 背景与目标

当前 `SYSTEM_PROMPT`（`agent.py:19-39`）只列出 9 个基础工具的文本摘要 + 6 条通用规则。模型虽然能经 tool schema 看到 `dispatch_subagent`/`use_skill`/`remember`/`recall`/`create_skill` 的存在，但提示词正文没有解释：

- **何时该用哪个工具**（选用启发式）；
- **技能（skill）是什么、怎么写、写在哪、长什么样、怎么用**；
- 记忆工具、派遣工具的适用场景。

同时，真实使用暴露了实际问题：一个由旧会话 `mv` 搬运到 `.code_agent/skills/` 的 SKILL.md **缺少 `---` frontmatter**，被 `SkillRegistry._read_skill` 判为无效而静默跳过，导致 Ctrl+S 显示"无可用技能"——说明 agent 此前没有"SKILL.md 必须带 frontmatter、创建必须走 `create_skill`"的规范引导。

**目标**：优化系统提示词，按**可用能力**条件注入四类指引（基础工具选用 / 技能使用与编写 / 记忆 / 子智能体派遣），让模型"会选工具、会写技能、会用记忆"；并修复出问题的 SKILL.md。

## 2. 迭代约束（用户决定）

- **注入策略 = 按能力条件注入**：基础工具选用指引常驻；技能指引仅在技能库非空时注入；技能**编写**指南仅 `memory=True`（`create_skill` 可用）时注入；记忆指引仅 `memory=True` 时注入；派遣指引仅 `allow_subagent=True` 时注入。提示词与模型实际可用工具保持一致，避免子智能体被提示它不能用的能力。
- 保持现有 marker：`Available skills`、`You are a subagent`（`SUBAGENT_PROMPT_EXTRA`）文本不变，兼容既有测试（`test_agent.py:289,366`）。
- 测试全离线；文档同步；记录 ADR-027。

## 3. 范围

**In scope**
- `code_agent/agent.py`：
  - `SYSTEM_PROMPT` 新增基础工具选用指引段（常驻）。
  - 新增模块级常量 `SKILL_GUIDANCE_USE` / `SKILL_GUIDANCE_CREATE` / `MEMORY_GUIDANCE` / `DISPATCH_GUIDANCE`（纯文本块）。
  - `__init__` 中按条件组装 `self._system_prompt`：`SYSTEM_PROMPT` +（`allow_subagent` 时）`DISPATCH_GUIDANCE` +（`skills is not None` 时）`SKILL_SECTION`（含现有 Available skills 列表 + `SKILL_GUIDANCE_USE`；若 `memory=True` 追加 `SKILL_GUIDANCE_CREATE`）+（`memory=True` 时）`MEMORY_GUIDANCE` +（`allow_subagent=False` 时）`SUBAGENT_PROMPT_EXTRA`。
- `test_workspace/.code_agent/skills/toutiao_top3_fetch/SKILL.md`：头部补 frontmatter（`name: toutiao_top3_fetch` + 一行 `description`），修复"无可用技能"。
- 测试、文档同步、ADR-027。

**Out of scope（本期不做）**
- 改变工具 schema / 注入时机逻辑（`skills.scan()` 为空时 `skills=None` 的判定等均不动）。
- 修改 `SUBAGENT_PROMPT_EXTRA`、`Tool` 类、`SkillRegistry`。
- 其它 SKILL.md / 提示词文案的国际化或模板化。

## 4. 设计

### 4.1 基础工具选用指引（常驻，并入 `SYSTEM_PROMPT`）

在原 `Rules` 后追加一段（中文可，与现有英文提示混排需统一——采用英文，保持现状语言）：

- Discover before reading: use `list_dir`/`glob` to locate files, `grep` to search contents; do not read whole files blindly.
- Read large files with `read_file` using `offset`/`limit`.
- Prefer `edit_file` (exact, unique substring) for surgical changes; use `write_file` for new files or whole-file rewrites.
- Verify with `run_command` (tests, compile, etc.).
- For external facts: `web_search` to discover URLs, then `web_fetch` to read the full page.

### 4.2 `SKILL_SECTION`（`skills is not None` 时注入）

现有块（`agent.py:200-207`）扩展为：

```
\n\nAvailable skills:\n<entries>\nUse the use_skill tool to load a skill when the task matches.

SKILL_GUIDANCE_USE（新增，恒随技能库注入）:
- use_skill(name) loads a skill's SKILL.md into context; follow its instructions.

SKILL_GUIDANCE_CREATE（新增，仅 memory=True 注入，因 create_skill 依赖 memory）:
- A skill = a SKILL.md file at <workdir>/.code_agent/skills/<name>/SKILL.md (project-level) or
  ~/.code_agent/skills/<name>/SKILL.md (user-level; project wins on name conflict).
- Format: frontmatter + markdown body:
    ---
    name: <letters/digits/-/_>
    description: one-line "when to use this"
    ---
    <reusable steps / gotchas / commands>
- Create it with create_skill(name, description, content) — it writes the file for you.
  .code_agent is a protected path: never use write_file/edit_file on skills.
- description is what the agent sees in the available-skills list and skill picker — make it matchable.
- Write a skill after finishing a reusable, non-trivial workflow (API endpoints, gotchas, verify commands).
```

### 4.3 `MEMORY_GUIDANCE`（`memory=True` 时注入）

- `recall(query, top_k)` searches project knowledge from prior sessions before re-reading long docs.
- `remember(content, tags)` saves durable facts/decisions/gotchas for future sessions.
- Relevant memories are auto-injected at task start and auto-summarized on success; no manual action needed.

### 4.4 `DISPATCH_GUIDANCE`（`allow_subagent=True` 时注入）

- `dispatch_subagent(task)` delegates an independent sub-task that does not need shared context; it returns a concise report. Do not use it for trivial single-call work.

### 4.5 组装顺序（`agent.py` `__init__`）

```
self._system_prompt = SYSTEM_PROMPT
    + (DISPATCH_GUIDANCE if allow_subagent else "")
    + SKILL_SECTION                          # if skills is not None
    + (MEMORY_GUIDANCE if memory else "")
    + (SUBAGENT_PROMPT_EXTRA if not allow_subagent else "")
```

- `SKILL_SECTION` 内部：`\n\nAvailable skills:\n<entries>\nUse the use_skill...` + `SKILL_GUIDANCE_USE` +（`memory=True` 时）`SKILL_GUIDANCE_CREATE`。
- `skills is None` 时不注入任何技能段；`memory=False` 时不注入编写指南与记忆段；`allow_subagent=False` 时无派遣段但有 `SUBAGENT_PROMPT_EXTRA`（子智能体）。

### 4.6 修复 SKILL.md

`test_workspace/.code_agent/skills/toutiao_top3_fetch/SKILL.md` 头部插入：

```
---
name: toutiao_top3_fetch
description: 抓取今日头条热榜 Top3 并整理为 Markdown 文档
---
```

正文不变（含脚本、接口、踩坑表）。此文件在 `test_workspace/`（不入库），仅本地测试用。

## 5. 测试

`tests/test_agent.py` 新增（全离线，mock 模型）：

1. `test_prompt_base_tool_guidance`：默认会话 system 含基础工具选用指引（如 `Discover before reading`）。
2. `test_prompt_dispatch_guidance_present/absent`：`allow_subagent=True` 含派遣指引；`allow_subagent=False` 不含、且含 `You are a subagent`。
3. `test_prompt_skill_guidance`：`skills` 注入时含 `SKILL_GUIDANCE_USE`；`skills+memory` 时含 `SKILL_GUIDANCE_CREATE`（示例 `create_skill`）；仅 `skills` 无 memory 时**不含**编写段。
4. `test_prompt_memory_guidance`：`memory=True` 含记忆指引（`recall`/`remember`）；默认不含。

## 6. 文档与 ADR

- `docs/architecture.md`：`agent.py` 小节补"系统提示词按能力条件组装"说明。
- `docs/design.md`：特性清单补一条。
- `.agent/03-decisions.md`：记录 ADR-027（系统提示词按能力注入指南 + SKILL.md 需 frontmatter 的教训）。

## 7. 验收

- `uv run pytest tests/ -v` 全绿（含新增 4 项）。
- `uv run python -m code_agent --workdir ../test_workspace --list-sessions` 正常；Ctrl+S 能列出 `toutiao_top3_fetch`（本地验证）。
- 凭据 grep 复核无命中。
