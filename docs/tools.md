# 工具定义与约定

> 本文档是工具的权威定义源（JSON Schema 的书写依据），随实现逐步完善。

## 1. 总则

- 所有工具均为**本地执行**，不依赖任何服务端托管能力。当前模型可见工具按条件共 9~14 个（read_file / write_file / edit_file / list_dir / run_command / glob / grep / web_fetch / web_search / dispatch_subagent；配置 skills 时另加 use_skill；`memory=True` 时另加 remember / recall / create_skill，均按条件注入）。
- 其中 9 个 stateless 工具的 schema 权威源为 `tools.py` 各 `Tool` 对象的属性（`name`/`description`/`parameters`/`required`）；`TOOL_SCHEMAS` 仍为派生导出（`[t.schema() for t in BASE_TOOLS]`）。`dispatch_subagent`/`use_skill` 为 agent.py 中的 session-bound `Tool` 子类（`DispatchSubagentTool`/`UseSkillTool`），`remember`/`recall`/`create_skill` 同为 agent.py 中的 session-bound `Tool` 子类（`RememberTool`/`RecallTool`/`CreateSkillTool`），其 schema 同样由对象属性派生。
- 工具机制：`Tool` 基类（Command 模式：schema 声明 + `validate()` + `execute()`）+ `ToolRegistry`（`register`/`get`/`schemas`/`execute`）。**新增工具 = 实现一个 `Tool` 子类 + `register`**；`bypass_policy=True` 跳过权限检查，`visible=False` 不注入 schema。
- 工具通过 OpenAI 原生 tool calling 接口暴露给模型。
- 所有工具返回统一格式 `ToolResult`（纯文本，便于回填对话历史）。

## 2. 统一返回格式

```json
{
  "ok": true,
  "output": "文本结果",
  "truncated": false,
  "exit_code": 0
}
```

- `ok`：工具是否成功执行。
- `output`：文本输出，超长自动截断（默认上限见下）。
- `truncated`：是否被截断。
- `exit_code`：仅命令类工具使用。

## 3. 核心工具清单

### 3.1 read_file
- 用途：读取文本文件内容。
- 参数：
  - `path`（string，必填）：文件路径（相对工作目录）。
  - `offset`（int，可选）：起始行号（1 起）。
  - `limit`（int，可选）：读取行数。
- 返回：文件内容（带行号可选）；文件不存在/不可读时 `ok=false` 并说明原因。
- 输出：内容整体读取后按输出长度上限（见 §4，默认 8000 字符）以"头+尾"方式截断并插入截断标记；`offset`/`limit` 提供行范围读取。

### 3.2 write_file
- 用途：创建新文件或整体覆盖已有文件。
- 参数：
  - `path`（string，必填）。
  - `content`（string，必填）。
- 返回：写入结果；父目录不存在时自动创建。
- 注意：覆盖是原子的（先写临时文件再 rename），避免写一半。

### 3.3 edit_file
- 用途：精确替换文件中的一段内容。
- 参数：
  - `path`（string，必填）。
  - `old_string`（string，必填）：待替换原文（必须唯一匹配）。
  - `new_string`（string，必填）：替换后的内容。
- 返回：替换结果；`old_string` 不存在或非唯一匹配时 `ok=false`，并回读上下文提示模型修正。
- 安全性：替换前后做校验，避免破坏文件。

### 3.4 list_dir
- 用途：列出目录内容。
- 参数：
  - `path`（string，可选，默认 `.`）。
- 返回：条目列表（名称 + 类型 + 大小）。跳过常见噪音（如 `.git` 内部、超大目录），防止输出爆炸。

### 3.5 run_command
- 用途：在工作目录下执行 shell 命令。
- 参数：
  - `command`（string，必填）。
- 返回：stdout、stderr、exit_code。
- 保护：
  - 默认超时（如 120s），超时终止并返回提示。
  - 输出长度上限，超长截断并标记 `truncated`。
  - 默认在项目工作目录内执行。

### 3.6 glob
- 用途：按 glob 通配符查找文件（支持 `**` 递归）。
- 参数：
  - `pattern`（string，必填）：glob 模式，如 `**/*.py`。
  - `path`（string，可选，默认 workdir）：搜索起点目录。
- 返回：匹配的【文件】路径列表（相对 workdir），排序确定性；只返回文件。
- 保护：跳过受保护路径（`.git`/`.env*` 除 `.env.example`）；结果上限 `MAX_SEARCH_RESULTS=500`，超出截断并标记 `truncated`。

### 3.7 grep
- 用途：在文件中做正则搜索。
- 参数：
  - `pattern`（string，必填）：正则表达式。
  - `path`（string，可选，默认 workdir）：文件或目录。
  - `include`（string，可选）：对文件名做 fnmatch 过滤（如 `*.py`）。
  - `ignore_case`（boolean，可选，默认 false）。
  - `output_mode`（string，可选，默认 `content`）：`content` / `files_with_matches` / `count`。
- 返回：
  - `content`：`相对workdir路径:行号:行内容`；单行超 200 字符截断。
  - `files_with_matches`：每行一个相对路径。
  - `count`：`相对路径:匹配行数`（按行计数，一行多次命中计 1）。
- 遍历：不跟随 symlink；跳过 `.git`、受保护路径、二进制文件（前 8192 字节含 NUL）、gitignore 忽略项。
- 限制：结果上限 500 条，超出截断并标记 `truncated`；无匹配返回 `(no matches)`。

#### 3.7.1 gitignore 基础支持
- 逐目录读取 `.gitignore`，规则沿路径从根向下累积，后加规则优先（最后匹配生效）。
- 支持：`#` 注释/空白行跳过；`!` 取反；`/` 锚定目录根；`dir/` 仅目录；普通 glob（`*` 不跨 `/`）。
- 注：`*` 通配经 Python fnmatch 实现，会跨 `/`，与 git 严格语义略有差异（如 `/sub/*.txt` 会匹配 `sub/deep/x.txt`）。
- 不支持（本期限制）：`**` 特殊模式、反斜杠转义完整集。

### 3.8 web_fetch
- 用途：抓取公网 http/https 页面，提取标题+正文+前 10 链接回传模型。
- 参数：
  - `url`（string，必填）：公网 http(s) 地址。
- 返回："Title + 正文 + Links（≤10）"，以换行分隔；空页面返回 `(empty page)`。
- 输出：整体按输出长度上限（默认 8000 字符）截断并标记 `truncated`。
- SSRF 保护：仅 http/https；字面 IP 直接判公网；localhost/.local 拒绝；配置 HTTP(S) 代理时按主机名放行（代理解析真实主机，兼容 fake-ip 代理），无代理时 DNS 解析后所有 IP 均须公网；重定向逐跳重校验；timeout 20s / max_bytes 2MB；仅 text/html|text/plain。

### 3.9 web_search
- 用途：关键词搜索（DuckDuckGo Lite，免 key），返回编号结果列表（标题 + 真实 URL + 摘要）供模型挑选，再用 web_fetch 抓取详情。
- 参数：
  - `query`（string，必填）：搜索关键词。
  - `max_results`（int，可选，默认 8）：返回结果数，clamp 1..10。
- 返回：编号列表（`N. 标题` + 真实 URL + 摘要 snippet ≤200 字符，snippet 超长截断补 `...`）；无结果返回 `(no results)`。
- 输出：整体按输出长度上限（默认 8000 字符）截断并标记 `truncated`。
- 后端：固定 `lite.duckduckgo.com/lite/` 主机，复用既有安全链路（代理感知 + 逐跳公网校验 + 限量 max_bytes），零新 SSRF 面；失败重试 3 次。
- 与 web_fetch 配合："搜索 → 抓取 → 查证"闭环——web_search 发现候选 URL，web_fetch 抓取全文（自带 SSRF 防护）。

### 3.10 dispatch_subagent
- 用途：把子任务委托给子智能体。子智能体运行自己的 agent 循环（**同步嵌套循环**，父会话阻塞等待），完成后只回传最终报告。
- 参数：
  - `task`（string，必填）：子任务描述。
- 返回：子智能体最终报告（整体按输出长度上限 8000 字符截断并标记 `truncated`）；子智能体无最终文本时返回 `(subagent returned no report; status: <reason>)`。
- 子智能体能力：继承父会话的 workdir/llm/policy/interact/skills（`use_skill` 可用），保留全部 9 个本地工具 + `use_skill`；`max_iterations=10`（`SUBAGENT_MAX_ITERATIONS`）。
- 阉割派遣（双层强制，深度恒 1）：
  1. 子会话 `allow_subagent=False`，其模型工具列表不含 `dispatch_subagent` schema（模型不可见、无法发起派遣）；
  2. 即便模型尝试调用，`DispatchSubagentTool.execute` 运行时对 `allow_subagent=False` 会话直接返回拒绝（`ToolResult(ok=False)`）；
  3. 子会话 system prompt 追加 subagent 指示（"You cannot delegate to sub-subagents"）。
- 权限继承：`policy`/`interact` 透传给子会话，`--deny`/`--ask` 规则对子智能体同样生效，防止绕过权限。
- 注意：派遣动作本身不经过 policy 检查（`DispatchSubagentTool` 的 `bypass_policy=True`；子会话内部工具调用仍继承 policy）；`dispatch_subagent` 不在 `BASE_TOOLS`/`TOOL_SCHEMAS` 中，由 agent.py 的 `DispatchSubagentTool` 提供并在 `AgentSession._registry` 条件注册（`visible=allow_subagent`）。
- 不持久化：子会话不携带 store/workspace，子任务对话不写入会话存储，回传仅最终报告，避免污染父上下文。
- 注意：本工具的 schema 由 `agent.py` 的 `DispatchSubagentTool`（session-bound `Tool` 子类，仅 `task` 参数）对象属性派生，不属于 `tools.py` 的 `BASE_TOOLS`/`TOOL_SCHEMAS`。

### 3.11 remember
- 用途：把一条可复用的项目知识（事实/决策/gotcha/关键文件位置）写入项目记忆库，供未来会话检索。属于"项目记忆与经验沉淀"（ADR-026）。
- 参数：
  - `content`（string，必填）：要记住的知识/事实/gotcha。
  - `tags`（string，可选）：逗号分隔的标签。
- 返回：`remembered: <记忆 id>`；空 content 返回 `ok=false`（`content is required`）。
- 存储：`<workdir>/.code_agent/memory/memories.jsonl`（JSONL，字段 id/content/tags/source_session/created_at/updated_at/usage_count）。
- 机制：记忆工具为 **session-bound** `Tool` 子类（agent.py 的 `RememberTool`），仅 `AgentSession(memory=True)` 时注册（`visible=True`，走 policy，不 bypass）；`memory=False` 时记忆工具不注册（unknown tool）。`.code_agent` 为受保护路径，模型不可经文件工具直写，只能走本工具（受保护路径由所有者直写）。

### 3.12 recall
- 用途：按关键词从项目记忆库召回相关历史知识（跨会话），供当前任务参考。
- 参数：
  - `query`（string，必填）：要搜索的内容。
  - `top_k`（int，可选，默认 3）：最多返回条数，clamp 1..10。
- 返回：编号列表（`N. 内容`，含 tags）；无命中返回 `(no relevant memories)`；整体按输出长度上限截断并标记 `truncated`。
- 机制：自实现关键词相关度打分（`_tokens` 拆词 + 词长加权，CJK 单字 + 英数字词），命中条目 bump `usage_count` 并落盘（热度影响排序，`-score, -usage_count`）；`memory=False` 时记忆工具不注册（unknown tool）。
- 与自动注入的关系：首任务到达时 agent 也会按任务相关性自动注入 top-K（≤3 条）system 记忆块（限量防爆上下文，见 context-management.md §9）；`recall` 是运行中的按需主动召回通道。

### 3.13 create_skill
- 用途：把可复用的工作流沉淀为项目级技能（SKILL.md），未来会话经 `use_skill` 加载。
- 参数：
  - `name`（string，必填）：技能名（仅字母/数字/`-`/`_`）。
  - `description`（string，必填）：简短描述。
  - `content`（string，必填）：Markdown 指令正文。
- 返回：`created skill: <name> (<path>)`；name 非法返回 `invalid skill name: <...>`；`skills is None` 时返回 `skills are not available`。
- 机制：复用 `SkillRegistry.add`（agent.py `CreateSkillTool`），写入 `<workdir>/.code_agent/skills/<name>/SKILL.md`，与 `use_skill` 同一技能库——沉淀后的技能立即对当前及未来会话可用。技能沉淀**仅**由模型显式 `create_skill` 触发，不走任务成功的自动沉淀通道。`.code_agent` 为受保护路径，模型不可经文件工具直写，只能走本工具。

## 4. 输出长度与安全约定

- 单次工具输出文本上限：默认 8000 字符（可配置），超出部分以"头+尾"方式截断并插入截断标记。
- 命令类工具禁止交互式等待（无 TTY），避免阻塞循环。
- 禁止执行破坏性高危操作策略由 system prompt 约束，不在此层强制（遵循题目允许范围）。

## 5. 未来扩展（暂不实现）

- 搜索性能：必要时可在工具内部将 grep 引擎替换为 ripgrep（封装不变，对外 schema 不动）。
- 并行工具调用：模型一次返回多个 tool_calls 时，串行→并行（按依赖性）。

## 6. 变更流程

- 修改本文件 → 同步更新 `tools.py` 中 JSON Schema → 更新 `tests` 中的用例。
