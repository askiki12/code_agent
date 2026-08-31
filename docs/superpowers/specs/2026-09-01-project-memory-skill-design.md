# 迭代设计：项目记忆与经验沉淀（remember/recall/create_skill + 自动注入与沉淀）

> 日期：2026-09-01 ｜ 状态：已批准 ｜ 关联 ADR：ADR-026（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/tools.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

当前 agent 无跨会话记忆：每次会话只靠 workdir 里的代码 + 会话文件，模型不记得"上次改过什么、为什么不用 X、这个项目有哪些坑"。用户期望一个**知识库 / RAG 记忆系统**级别的特色功能。本项目此前也没有真正的检索增强（RAG）：文档读取是模型手动驱动的 `read_file`/`grep`，长文档靠分段读，无自动检索注入。

**目标**：实现 **agent 原生的项目记忆与经验沉淀系统**：
- **跨会话记忆**：agent 可显式 `remember` 写入、`recall` 检索项目级记忆；任务开始时自动注入 top-K 相关记忆。
- **经验沉淀**：任务成功结束后自动把"本项目关键知识"总结写入记忆；技能（更结构化的可复用流程）由模型显式 `create_skill` 沉淀，下次 `use_skill` 即用。
- **自实现检索**（红线：零新依赖）：关键词相关度打分，不用向量库。

## 2. 迭代约束（用户决定）

- **注入时机**：首个用户消息（任务）到达时，按任务对记忆库打分，注入 top-K（≤3 条）system 上下文；运行中模型可 `recall(query)` 主动召回。
- **沉淀方式（方案 C 混合）**：工具驱动为主（模型主动 `remember`/`create_skill`）；任务**成功结束**（`finished=True`）时做**一次**自动"要点总结"写入记忆（`source_session` 标注）；技能沉淀仅由模型显式 `create_skill` 触发。
- **默认关闭**：`AgentSession(memory=False)` 默认关（保既有测试不触发额外 LLM 调用）；`cli.main` 开启；子智能体 `memory=False`（不注入/不总结/无记忆工具）。
- 检索零新依赖（自实现打分）；凭据不入库；测试全离线。

## 3. 范围

**In scope**
- `code_agent/memory.py`（新）：`MemoryEntry` + `MemoryStore`（JSONL 存取、原子写、关键词相关度打分、usage_count 热度）。
- `code_agent/skills.py`：`SkillRegistry.add(name, description, content)`（写项目级 `<workdir>/.code_agent/skills/<name>/SKILL.md`，frontmatter 格式与 scan 一致，name 安全校验）。
- `code_agent/agent.py`：session-bound `RememberTool`/`RecallTool`/`CreateSkillTool`（走 policy，非 bypass）；`AgentSession(memory=False)`、`_memory`/`_memory_injected`；`run_task` 首任务注入 + 成功结束自动总结；`_remember`/`_recall`/`_create_skill` 方法。
- `code_agent/cli.py`：`AgentSession(memory=True)`。
- 测试、文档同步、ADR-026。

**Out of scope（本期不做）**
- 向量化检索 / 语义 embedding（零依赖约束）。
- 记忆过期清理、去重合并、跨项目记忆。
- 自动总结的流式输出 / UI 展示（静默后台执行）。
- 用户手动管理记忆的 CLI 命令。

## 4. 设计

### 4.1 `memory.py`

```
@dataclass MemoryEntry:
    id: str                 # code_agent-mem-<%Y%m%d-%H%M%S%f>（微秒，防碰撞）
    content: str
    tags: list[str]         # 默认 []
    source_session: str     # 来源会话 id（可空）
    created_at: str         # isoformat microseconds
    updated_at: str
    usage_count: int        # 默认 0；recall 命中 +1

class MemoryStore:
    def __init__(self, root)          # root = <workdir>/.code_agent/memory
    def _path() -> str                # <root>/memories.jsonl
    def all() -> list[MemoryEntry]    # 全量加载（坏行跳过）
    def add(content, tags=None, source_session="") -> MemoryEntry   # 追加 + 原子写
    def recall(query, top_k=3) -> list[MemoryEntry]  # 打分排序，命中 usage_count+1 并原子写
    @staticmethod _tokens(text) -> list[str]   # 小写 + 非字母数字切分 + CJK 单字成 token
    @staticmethod _score(entry, query_tokens) -> int   # 查询 token 在内容中出现次数加权
```

- 存储：单文件 `<root>/memories.jsonl`，全量读入内存，每次变更原子写（tmp + os.replace，复用 session.py 模式）。项目级记忆量小，全量重写可接受。
- `recall`：对每条记忆算 `_score`，过滤 score>0，按 `(score desc, usage_count desc)` 取 top_k；对返回的记忆 `usage_count += 1` 并保存。
- `_tokens`：`re.findall(r"[a-zA-Z0-9_]+", text.lower())` + 对每个 CJK 字符单独作为 token。
- `_score`：`sum(len(t) for t in query_tokens if t in entry_tokens)`（长词命中权重大）。

### 4.2 `skills.py` 新增

```
def add(self, name, description, content) -> str:
    # name 安全校验：仅 [a-zA-Z0-9_-]，禁止路径分隔/`.`；非法抛 ValueError
    # 写入 <project_dir>/.code_agent/skills/<name>/SKILL.md
    # frontmatter: ---\nname: X\ndescription: Y\n---\n<content>
    # 返回写入路径；已存在同名则覆盖（项目级优先语义）
```

### 4.3 `agent.py`：记忆工具 + 注入 + 沉淀

**三个 session-bound 工具**（走 policy，`bypass_policy=False`；visible = `self.memory`）：

```
class RememberTool(Tool):
    name="remember"; 参数 {content: str 必填, tags: str 可选}
    execute → session._remember(args)
class RecallTool(Tool):
    name="recall"; 参数 {query: str 必填, top_k: int 可选默认3}
    execute → session._recall(args)
class CreateSkillTool(Tool):
    name="create_skill"; 参数 {name, description, content 必填}
    execute → session._create_skill(args)
```

**`AgentSession` 新增**：
- `__init__` 参数 `memory: bool = False`；`self._memory = MemoryStore(os.path.join(workdir, ".code_agent", "memory")) if memory else None`；`self._memory_injected = False`。
- registry 追加三个记忆工具（`visible=self.memory`）；`_remember`/`_recall`/`_create_skill` 方法（`_memory is None` 时返回 `ToolResult(ok=False, output="memory is disabled")`）。
- `new_session()` / `load_session()`：`self._memory_injected = False`。
- `run_task`：
  - **注入**：`add_user(task)` 后、循环前，若 `self._memory` 且未注入：`entries = self._memory.recall(task, top_k=3)`；若命中，`self.conversation.add_system(_memory_block(entries))`；置 `_memory_injected=True`。`_memory_block` 格式：
    ```
    [Project memory]
    Prior sessions recorded about this project (use recall for more; remember to save new knowledge):
    - <content>
    - ...
    ```
  - **自动沉淀**：`finally` 段，会话保存后，若 `self._memory` 且本轮 `finished=True`：调用 `_auto_memorize()`（整体 try/except 静默）。**实现要求**：`run_task` 循环有多个 return 点，需把循环返回值捕获到 finally 可及的局部变量（如把循环体提取为私有 `_run_loop()` 再在 `finally` 后 `return result`，或改为"循环内赋值 + finally 后统一 return"），保证 finally 能判断 `result.finished`。`_auto_memorize` 用 `self.llm.chat` 发一次总结请求：
    ```
    system: You are an agent that just finished a task in a coding project.
    user: Extract 1-3 durable, reusable pieces of project knowledge from this conversation
    (facts, decisions, gotchas, key file locations). Reply with a JSON array of strings only.
    <最近对话截取：最后至多 40 条消息，每条 "role: 前 200 字符"，总长 ≤ 6000 字符>
    ```
    解析：`json.loads` 得 list[str] → 每条 `self._memory.add(c, source_session=self.session_id or "")`；解析失败 → 整段作为一条；任何异常静默跳过。
- **子智能体**：`_dispatch_subagent` 构造子会话时 `memory=False`（不注入/不总结/无记忆工具）。

### 4.4 `cli.py`

`AgentSession(...)` 构造追加 `memory=True`。

## 5. 行为保留核对

| 现状行为 | 重构后如何保留 |
|---|---|
| 既有 agent 测试不触发额外 LLM 调用 | `memory=False` 默认关，自动总结/注入/工具全部不生效 |
| `use_skill`/`dispatch_subagent` 语义 | 不变（记忆工具是新增，不与它们冲突） |
| 受保护路径 `.code_agent/` | 记忆/技能由 `MemoryStore`/`SkillRegistry` 所有者直写，`read_file`/`write_file` 仍拒 |
| policy 语义 | 记忆工具走 policy（可 `--deny remember:*`）；编排工具仍 bypass |
| 子智能体能力阉割 | 不变；子会话 `memory=False` 无记忆工具 |

## 6. 测试计划（全部离线）

- `test_memory.py`（新）：`MemoryStore` add/all/recall（打分排序、usage_count+1、无命中→空、top_k 截断）、tags/source_session 持久化、坏行跳过、原子写。
- `test_skills.py`：`SkillRegistry.add`（写文件、frontmatter、scan 能发现、非法 name 抛 ValueError、同名覆盖）。
- `test_agent.py`：
  1. `memory=False` 默认：schemas 不含 remember/recall/create_skill；`_run_tool` 调用返回 "unknown tool" 或 "memory is disabled"（按实现）。
  2. `memory=True`：schemas 含三个记忆工具；`_run_tool(remember/recall/create_skill)` 正常。
  3. 自动注入：首个任务后 conversation 含 "[Project memory]" 块（预置记忆 + 相关任务）；再次任务不重复注入。
  4. 自动沉淀：成功结束 + 假 LLM 对总结请求返回 JSON → `MemoryStore.all()` 新增条目（source_session 正确）；失败结束不沉淀。
  5. 子智能体 `memory=False`。
- `test_cli.py`：`main` 构造 AgentSession 传 `memory=True`。
- 全量回归 + `--help` + 凭据 grep。

## 7. 文档同步

- `architecture.md`：memory.py（MemoryStore/MemoryEntry）、agent.py（记忆工具/注入/沉淀）、skills.py（add）、cli（memory=True）。
- `tools.md`：新增 §3.11-3.13 remember/recall/create_skill（参数/返回/与 skill 机制关系）。
- `context-management.md`：自动注入（top-K system 块、限量）。
- `design.md`：§6 勾选 + §8 追加（ADR-026）。
- `development.md`：TUI/CLI 说明、测试目录。
- `.agent/03-decisions.md`：**ADR-026**（不入库）。

## 8. 开发顺序（小步推进，每步可验证）

1. `memory.py` MemoryStore + `test_memory.py`（TDD）
2. `skills.py` add + `test_skills.py`（TDD）
3. `agent.py` 记忆工具 + 注入 + 自动沉淀 + 子智能体关闭 + `test_agent.py`（TDD）
4. `cli.py` memory=True + `test_cli.py`
5. 文档同步 + ADR-026
6. 全量回归 + 凭据复核 + 提交

## 9. 后续（本次迭代之后）

更新 `human-in-the-loop-role.md`（追加本轮）与 `software-engineering-highlights.md`（记忆/RAG 特色亮点），可作演示视频素材。
