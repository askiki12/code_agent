# 迭代设计：会话持久化 + 多会话管理

> 日期：2026-08-29 ｜ 状态：已批准 ｜ 关联 ADR：ADR-012（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

当前 `Conversation`（context.py）仅存在于内存：交互模式退出即丢失，无法新建/列出/恢复会话，
也没有任何持久化。业界成熟产品（Claude Code JSONL sessions、OpenCode session 树）均支持会话管理。

**目标**：对话持久化到本地，支持新建/列出/恢复会话；交互模式跨重启可续接；为后续 checkpoint/skill 打地基。

## 2. 范围

**In scope**
- 会话存储：`<workdir>/.code_agent/sessions/<session_id>.jsonl`（JSONL，首行 meta，其后消息）。
- `SessionStore`：`list_sessions` / `create` / `save` / `load`，纯标准库 json。
- `Conversation` 序列化/反序列化（`to_jsonl` / `from_jsonl`），恢复后重注入当前 SYSTEM_PROMPT。
- `AgentSession` 集成：可选 `store` + `session_id`，每次 `run_task` 结束自动保存；支持恢复既有会话。
- CLI：`--list-sessions` / `--resume <id>`；交互模式斜杠命令 `/new` `/list` `/resume <id>`（`/exit` 沿用现有 exit/quit）。
- 安全：`.code_agent` 加入受保护路径；`.gitignore` 忽略 `.code_agent/`。
- 测试、文档同步、ADR-012、真实 API 冒烟。

**Out of scope（本期不做）**
- 文件快照/回退（Claude Code `/rewind`）。
- AI 生成会话标题（用首条任务截断）。
- 会话树/fork、加密存储、服务端同步。
- 自动摘要压缩历史。

## 3. 存储格式与位置

- 根目录：`<workdir>/.code_agent/sessions/`。
- 文件名：`<session_id>.jsonl`；`session_id = strftime("code_agent-%Y%m%d-%H%M%S%f")`（微秒精度，可读、可排序、同秒不碰撞）。
- 首行 meta（JSON）：`{"type":"meta","id":..., "title":..., "created_at":..., "updated_at":...}`。
- 其后每行一条 OpenAI 兼容消息 dict（与 `Conversation._messages` 结构一致，含 `role`/`content`/`tool_calls`/`tool_call_id`/`name`），`json.dumps(..., ensure_ascii=False)`。
- 标题：首条 `user` 任务内容去换行后前 40 字符。
- 保存：每次 `run_task` 结束后全量原子重写（tmp 文件 + `os.replace`），更新 `updated_at`。

## 4. SessionStore API（新增 `code_agent/session.py`）

```
class SessionStore:
    def __init__(self, root: str) -> None       # root = <workdir>/.code_agent/sessions
    def list_sessions(self) -> list[dict]       # 扫描 *.jsonl，读首行 meta，返回
                                                #   {id,title,created_at,updated_at,message_count}
                                                #   按 updated_at 倒序；目录不存在返回 []
    def create(self, title: str) -> str         # 新建空 jsonl（写 meta 行），返回 session_id
    def save(self, session_id: str, messages: list[dict], title: str | None = None) -> None
                                                # 全量原子写；文件不存在视为 create（保留原 created_at）
    def load(self, session_id: str) -> tuple[dict, list[dict]]
                                                # 返回 (meta, messages)；文件不存在 raise KeyError；
                                                # 坏行跳过并累计警告（不中断）
```

- `save` 写失败（磁盘/权限）→ 抛 `OSError`，由调用方捕获并打印警告（不崩溃 agent）。

## 5. Conversation 序列化（context.py 扩展）

- `to_jsonl(self) -> str`：逐行 `json.dumps(msg, ensure_ascii=False)`。
- `from_jsonl(text: str, system_prompt: str | None = None) -> Conversation`（classmethod）：
  - 逐行解析；非 dict 行跳过；坏 JSON 行跳过。
  - 若 `system_prompt` 给定：移除原有 `system` 消息，在消息列表最前插入当前 `system_prompt`。
  - 构造完成后调用 `is_valid()` 校验，返回的实例保持结构有效。

## 6. AgentSession 集成（agent.py）

- `__init__` 新增可选参数：`store: SessionStore | None = None`、`session_id: str | None = None`、`resume: bool = False`。
  - `resume=True` 且 `session_id` 给定：从 `store.load` 重建 Conversation（重注入 SYSTEM_PROMPT）。
  - `resume=True` 但 `session_id` 未给 → `ValueError`（明确报错，不静默新建）。
  - 否则新建 Conversation，并在 `run_task` 首次调用时 `store.create` 生成 `session_id`（仅创建一次）。
- `run_task` 正常结束/终止后：若 `store` 存在，调用 `store.save(self.session_id, self.conversation.messages, title)`；标题取首个 user 消息截断。
- 不改变无 store 时的既有行为（测试向后兼容）。

## 7. CLI（cli.py）

- 新参数：
  - `--list-sessions`：列出当前 workdir 的会话，打印 id / 标题 / 消息数 / 更新时间，退出。
  - `--resume <id>`：恢复指定会话。
- 组合规则：
  - `--list-sessions` 与 `--prompt`/`--interactive` 互斥（若同给，`--list-sessions` 优先并报错提示）。
  - `--resume <id>` 可与 `--prompt` 组合（恢复后执行一次性任务）或 `--interactive` 组合（恢复后进入交互）。
  - 无 `--resume` 时默认新建会话并自动保存。
- 交互模式斜杠命令（输入以 `/` 开头时优先匹配）：
  - `/new`：新建会话（旧会话已自动保存）。
  - `/list`：列出会话。
  - `/resume <id>`：加载并切换会话（当前会话先保存）。
  - `/exit`：退出（等价现有 exit/quit）。
- 交互模式每次任务后显示当前 `session_id`（轻量提示）。
- 会话目录不存在时自动创建（`os.makedirs(exist_ok=True)`）。

## 8. 安全

- `tools.py` `_is_protected_path`：新增路径组件 `.code_agent`（任何组件为 `.code_agent` 即禁读禁写，含 glob/grep 遍历）。
- `code_agent/.gitignore`：追加 `.code_agent/`（会话含任务内容，不入库）。

## 9. 错误处理

- `load` 文件不存在 → `KeyError`；CLI 捕获并打印 `session not found: <id>`（stderr，退出码 1）。
- 损坏行 → 跳过 + 计数，恢复继续（记录警告）。
- `save` 失败 → CLI/agent 捕获，stderr 警告，不崩溃。
- 斜杠命令参数缺失/非法 → 打印用法提示，不退出。

## 10. 测试计划

**test_session.py（新增）**
1. create 生成合法文件（首行为 meta，含 id/title/时间）
2. save + load 往返消息一致
3. list_sessions：多个会话按 updated_at 倒序、message_count 正确
4. list_sessions：目录不存在返回空列表
5. load 不存在 → KeyError
6. load 损坏行跳过（坏行不中断、合法行保留）
7. save 覆盖已有会话保留 created_at、更新 updated_at

**test_context.py（扩展）**
8. to_jsonl/from_jsonl 往返（含 tool_calls 消息）内容一致
9. from_jsonl 移除旧 system 并插入当前 system
10. from_jsonl 恢复后 is_valid 为 True（含完整 tool 往返）

**test_agent.py（扩展）**
11. AgentSession 带 store：run_task 后会话被保存，消息含该轮往返
12. resume=True 恢复既有会话（system 重注入、消息载入）
13. 无 store 时行为不变（既有测试不破坏）

**test_cli.py（扩展）**
14. `--list-sessions` 输出包含会话 id/标题
15. `--resume <id>` 参数解析与缺 id 报错
16. 交互斜杠命令 `/new` `/list` `/resume` 路由（mock input）
17. `--resume` 不存在的 id → stderr 报错、退出码 1

**test_tools.py（扩展）**
18. `.code_agent` 受保护：read/write/grep/glob 均拒绝或跳过

全部离线。冒烟（真实 API）：`--prompt` 建会话 → 重启 `--resume` 续接对话。

## 11. 文档同步

- `docs/architecture.md`：模块总览加 `session.py`；数据流/接口约定更新。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加。
- `docs/development.md`：新 CLI 参数与斜杠命令、测试数更新。
- `docs/context-management.md`：新增"会话持久化"章节。
- `code_agent/docs/superpowers/specs/2026-08-29-session-persistence-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-012（不入库，ADR-007）。

## 12. 开发顺序（小步推进，每步可验证）

1. `session.py` + `test_session.py`（TDD）
2. `context.py` 序列化 + `test_context.py`（TDD）
3. `agent.py` 集成 + `test_agent.py`（TDD）
4. `cli.py` 参数/斜杠命令 + `test_cli.py`（TDD）
5. 安全（tools.py 受保护 + .gitignore）+ `test_tools.py`
6. 文档同步 + ADR-012
7. 真实 API 冒烟（建会话 → resume 续接）
8. 全量回归 + 凭据复核 + 提交
