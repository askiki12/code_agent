# 迭代设计：工作区一等公民

> 日期：2026-08-29 ｜ 状态：已批准 ｜ 关联 ADR：ADR-013（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

当前 `--workdir` 仅作为工具执行的工作目录，无项目身份、无跨启动状态；会话虽已持久化到
`<workdir>/.code_agent/sessions/`，但目录本身不是"一等公民"（无元数据、无项目概况、无上次会话记忆）。
业界（Claude Code / OpenCode / Codex）均以当前目录为工作区身份，并展示项目概况。

**目标**：让 `--workdir` 成为一等公民——持久化工作区身份与最近会话，交互启动展示项目概况并提示续接，
为后续项目级配置（权限规则等）打地基。

## 2. 范围

**In scope**
- `<workdir>/.code_agent/workspace.json` 工作区元数据（id/name/path/created_at/updated_at/last_session_id）。
- 新增 `code_agent/workspace.py`：`Workspace` 类（幂等初始化、原子写、损坏容错、touch_session、display）。
- `AgentSession` 可选 `workspace` 参数：每次 `run_task` 结束后调用 `touch_session(session_id)`。
- CLI：任意模式启动幂等初始化；交互模式启动展示工作区行 + 续接 Tip；`--prompt` 静默。
- 测试、文档同步、ADR-013、真实 API 冒烟。

**Out of scope（本期不做）**
- 项目级配置（默认 max_iterations、权限规则、hooks 等）——仅留元数据地基。
- 工作区列表/切换命令（`/workspace`）、多工作区管理。
- workspace 与远端仓库绑定/同步。
- AI 生成工作区描述。

## 3. 存储格式与位置

- 文件：`<workdir>/.code_agent/workspace.json`（`.code_agent/` 已为受保护路径，gitignore 已忽略）。
- 内容：
  ```json
  {
    "id": "<sha1(realpath(workdir))[:12]>",
    "name": "<目录 basename>",
    "path": "<绝对 realpath>",
    "created_at": "<isoformat>",
    "updated_at": "<isoformat>",
    "last_session_id": "<session_id | 省略>"
  }
  ```
- `id`：`sha1(os.path.realpath(workdir))[:12]`——同一目录跨启动稳定。
- `name`：目录 basename。
- `created_at`：首次初始化时间（重复初始化不覆盖）。
- `updated_at`：每次 `touch_session` 刷新。
- `last_session_id`：最近一次会话 id（无会话则省略）。
- **`session_count` 不持久化**：展示时由调用方经 `store.list_sessions()` 实时获取，避免计数漂移。
- 写操作为原子写（tmp + `os.replace`）。

## 4. Workspace API（新增 `code_agent/workspace.py`）

```
class Workspace:
    def __init__(self, workdir: str) -> None
        # 读取 <workdir>/.code_agent/workspace.json；不存在则初始化（幂等）；
        # json 损坏/结构非法 → 打印警告并重建（保留新身份）

    @property
    def data(self) -> dict            # 返回副本 {id,name,path,created_at,updated_at,last_session_id?}

    def touch_session(self, session_id: str) -> None
        # 更新 last_session_id + updated_at，原子写

    def display(self) -> str          # 基础行文本：
                                      # "Workspace: <name> (<id>)"
                                      # 不含 sessions/last（实时数据由 CLI 拼接）
```

- `__init__` 中若文件存在但 json 解析失败或 `type` 校验失败 → 警告 + 重建。
- `touch_session` 写失败（OSError）→ 由调用方捕获并警告（不崩溃，同 ADR-012 save 模式）。

## 5. AgentSession 集成（agent.py）

- `__init__` 新增可选参数 `workspace: Workspace | None = None`。
- `run_task` 的 `finally:` 保存块中：若 `workspace` 存在，在 store.save 之后调用
  `self.workspace.touch_session(self.session_id)`（仅在 `self.session_id` 已生成时）。
- 无 workspace 时行为完全不变。

## 6. CLI（cli.py）

- `main`：`_load_dotenv` 后、构造 AgentSession 前，`workspace = Workspace(workdir)`（幂等初始化，任意模式）。
- 交互模式启动横幅前打印工作区行（`workspace.display()` 基础行 + CLI 拼接实时统计）；若有 `last_session_id` 追加：
  ```
  Workspace: <name> (<id>) | sessions: <n> | last: <last_session_id>
  Tip: /resume <last_session_id> 续接上次会话
  ```
  - `<n>` = `len(store.list_sessions())`。
  - Tip 仅当存在 last_session_id 且对应会话文件仍存在时打印。
- `--prompt` 一次性：不打印工作区行（静默）。
- `--list-sessions`：仍只列会话，不打印工作区行。
- 交互模式每次任务后已有的 `[session <id>]` 提示保留。

## 7. 安全

- `workspace.json` 位于 `.code_agent/`（上一迭代已受保护，工具层禁读禁写，gitignore 已忽略）。
- 无新凭据/敏感内容（仅路径与时间戳；path 为本地绝对路径，随工作区）。

## 8. 错误处理

- workspace.json 缺失 → 初始化。
- workspace.json 损坏/非法 → stderr 警告 + 重建。
- `touch_session` 写失败 → stderr 警告，不崩溃（与 ADR-012 save 一致）。

## 9. 测试计划

**test_workspace.py（新增）**
1. 构造即生成 workspace.json；重复构造幂等（created_at 不变）。
2. data 结构正确：id = sha1(realpath)[:12]、name = basename、path = realpath。
3. 同一 workdir 跨实例 id 稳定（两次构造 id 相同）。
4. touch_session 更新 last_session_id 与 updated_at。
5. 损坏 json → 警告 + 重建为合法结构（重新构造后 data 可用）。

**test_agent.py（扩展）**
6. 带 workspace 时 run_task 后 last_session_id 被更新为 session_id；无 workspace 行为不变。

**test_cli.py（扩展）**
7. 交互模式启动输出包含 "Workspace:" 行；有 last_session_id 时含 Tip。
8. `--prompt` 输出不含 "Workspace:" 行（静默）。

全部离线。冒烟（真实 API）：同一 workdir 两次启动（--prompt 建会话 → 再 --interactive 或 --prompt），
验证第二次启动工作区 id 相同且 last_session_id 提示正确。

## 10. 文档同步

- `docs/architecture.md`：模块总览加 `workspace.py`；§3 接口约定（Workspace API）。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加。
- `docs/development.md`：交互模式启动展示说明（工作区行 + Tip）。
- `code_agent/docs/superpowers/specs/2026-08-29-workspace-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-013（不入库，ADR-007）。

## 11. 开发顺序（小步推进，每步可验证）

1. `workspace.py` + `test_workspace.py`（TDD）
2. `agent.py` 集成 + `test_agent.py`（TDD）
3. `cli.py` 展示 + `test_cli.py`（TDD）
4. 文档同步 + ADR-013
5. 真实 API 冒烟（同一 workdir 两次启动验证稳定性）
6. 全量回归 + 凭据复核 + 提交
