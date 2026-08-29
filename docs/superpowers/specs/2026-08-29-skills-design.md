# 迭代设计：skill 机制（可扩展技能库）

> 日期：2026-08-29 ｜ 状态：已批准 ｜ 关联 ADR：ADR-015（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

agent 目前只有固定 system prompt 与固定工具，无法承载领域专长/可复用工作流。业界（Claude Code/OpenCode）
通过 SKILL.md 技能让模型按需加载预定义指令。本迭代为 agent 增加可扩展 skill 机制：按需加载技能指令执行。

**目标**：支持从项目级与用户级目录扫描 skill（`<name>/SKILL.md`），system prompt 注入技能列表，
agent 通过 `use_skill` 工具按需加载 SKILL.md 全文并遵循执行。

## 2. 范围

**In scope**
- 新增 `code_agent/skills.py`：`SkillRegistry`（扫描项目级 + 用户级、frontmatter 解析、按名加载）。
- system prompt 注入可用技能列表（`Available skills: <name> - <description>`）。
- 新工具 `use_skill(name)`：加载 SKILL.md 全文返回模型；专用加载通道（不走 read_file 受保护限制）。
- 无技能时 `use_skill` 不注册（保持兼容）。
- 测试、文档同步、ADR-015、真实 API 冒烟。

**Out of scope（本期不做）**
- skill 附带脚本/参数执行（仅纯 markdown 指令文本，YAGNI）。
- 子代理（subagent）执行技能。
- 插件 hooks（tool.execute.before/after）。
- skill 在线安装/市场。

## 3. 存储与发现

- 目录：项目级 `<workdir>/.code_agent/skills/<name>/SKILL.md`、用户级 `~/.code_agent/skills/<name>/SKILL.md`。
- 合并：同名时项目级优先（用户级被项目级覆盖）。
- SKILL.md 格式：
  ```markdown
  ---
  name: <slug>
  description: <一句话描述>
  ---
  <markdown 正文：技能指令/工作流>
  ```
- `name` 校验：非空、无空白字符（slug）；`description` 非空。
- 非法（缺 frontmatter、name/description 缺失、目录下无 SKILL.md）→ 跳过并 stderr 警告。
- 技能列表按 name 排序。

## 4. SkillRegistry API（新增 `code_agent/skills.py`）

```
class Skill:
    name: str
    description: str
    path: str          # SKILL.md 绝对路径

class SkillRegistry:
    def __init__(self, project_dir: str, user_dir: str | None = None) -> None
        # user_dir 默认 ~/.code_agent/skills

    def scan(self) -> list[Skill]           # 项目 + 用户合并，按 name 排序，同名项目优先

    def load(self, name: str) -> str | None # 返回 SKILL.md 全文（含 frontmatter）；不存在返回 None

    @staticmethod
    def parse_frontmatter(text: str) -> tuple[str, str, str] | None
        # 解析 frontmatter → (name, description, body)；非法返回 None
```

- 目录缺失 → 视为空。
- `load` 按同名合并规则定位项目级或用户级文件。

## 5. 集成（agent.py）

- `AgentSession.__init__` 新增 `skills: SkillRegistry | None = None`。
- system prompt 动态化：构造时 `self._system_prompt = SYSTEM_PROMPT + skills_section`，其中
  `skills_section` 在 skills 存在时追加：
  ```
  \n\nAvailable skills:
  - <name>: <description>
  Use the use_skill tool to load a skill when the task matches.
  ```
  无 skills 时 `skills_section` 为空（`self._system_prompt == SYSTEM_PROMPT`）。
- `__init__` / `new_session` / `load_session` 均改用 `self._system_prompt`（而非模块级 `SYSTEM_PROMPT`）。
- `llm.chat(tools=...)`：若 skills 存在，`tools=TOOL_SCHEMAS + [_USE_SKILL_SCHEMA]`（`_USE_SKILL_SCHEMA` 为 agent.py 内定义的 use_skill schema）。
- `_run_tool` 对 `use_skill` 分支：
  ```python
  if tc.name == "use_skill" and self.skills is not None:
      return self._use_skill(tc.arguments)
  ```
- `_use_skill(arguments) -> ToolResult`：
  - 无 name → `ToolResult(ok=False, output="skill name is required")`
  - `content = self.skills.load(name)`；None → `ToolResult(ok=False, output=f"skill not found: {name}")`
  - 命中 → `ToolResult(ok=True, output=content)`
- 无 skills 时：`use_skill` 不注册，`_run_tool` 不分支（走 execute → unknown tool）。

## 6. use_skill 工具定义

```
name: use_skill
description: Load a skill's instructions into context. Returns the skill content; follow it.
参数:
  name (string, 必填): skill 名称
```

- 专用加载通道：直接经 `SkillRegistry.load` 读取技能文件，不经过 read_file 的受保护路径检查
  （技能目录在 `.code_agent/` 下，read_file 禁止；use_skill 是 agent 的受控技能入口）。
- name 参数仅用于查 registry，不暴露任意路径（无路径穿越面）。

## 7. 安全

- `use_skill` 仅能加载已注册的 skill（registry 内），不接受任意路径。
- 技能内容为模型指令（非代码执行），不引入新执行面。
- `.code_agent/` 受保护不变；技能文件由 registry 专门读取。
- 无凭据处理。

## 8. 错误处理

- 非法 SKILL.md → 跳过 + 警告。
- `load` 不存在 → None → `ToolResult(ok=False)`。
- 缺 name 参数 → `ToolResult(ok=False)`。
- 读取失败（OSError）→ `ToolResult(ok=False)`。

## 9. 测试计划

**test_skills.py（新增）**
1. scan：项目级 + 用户级合并，按 name 排序。
2. 同名覆盖：项目级优先于用户级。
3. parse_frontmatter 正常解析（name/description/body）。
4. 非法：缺 frontmatter / 缺 name / 缺 description → None。
5. load 返回正文；不存在 → None。
6. 空/缺失目录 → 空列表。

**test_agent.py（扩展）**
7. 带 skills：system prompt 含 "Available skills" 与技能名。
8. `use_skill` 调用 → tool 消息含 SKILL.md 内容。
9. skill 不存在 → tool 消息含 "skill not found"。
10. 无 skills：system prompt 不含技能段，`use_skill` 走 execute → unknown tool（ok=False）。

**test_tools.py（扩展）**
11. 无 skills 场景 `execute("use_skill", ...)` → unknown tool（兼容路径已在 agent 层挡，tools 层未知）。

全部离线。冒烟（真实 API）：创建示例 skill（如 `code-review`），让 agent 用 `use_skill` 加载并遵循执行一个小任务。

## 10. 文档同步

- `docs/architecture.md`：模块总览加 `skills.py`；§3 接口约定（SkillRegistry/use_skill）。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加。
- `docs/development.md`：技能目录结构、SKILL.md 编写格式、示例与运行说明。
- `code_agent/docs/superpowers/specs/2026-08-29-skills-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-015（不入库，ADR-007）。

## 11. 开发顺序（小步推进，每步可验证）

1. `skills.py` + `test_skills.py`（TDD）
2. `agent.py` 集成（system prompt 注入 + use_skill）+ `test_agent.py`（TDD）
3. `test_tools.py` 兼容用例
4. 文档同步 + ADR-015
5. 真实 API 冒烟（use_skill 加载并执行）
6. 全量回归 + 凭据复核 + 提交
