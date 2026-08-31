# 迭代设计：TUI 可观测性（token/上下文/缓存率）+ 会话重命名

> 日期：2026-08-31 ｜ 状态：已批准 ｜ 关联 ADR：ADR-023（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/context-management.md`、`docs/development.md` 与 `docs/session.md`（如新增）为接口/使用权威源。

## 1. 背景与目标

当前 TUI 无法感知上下文占用与缓存状态：用户不知道一次会话消耗了多少 token、离模型上下文上限/裁剪预算还有多远、服务端 prompt 缓存命中多少；会话标题只能由首个用户消息自动生成，无法手动命名。

**目标**：
1. **可观测性**：TUI 状态栏常驻显示真实 token 占用（占预算百分比）与缓存命中率；数据优先取真实 API usage，provider 不支持时回退本地启发式估算。
2. **上下文窗口**：自动解析模型上下文窗口 W（`/models` → 模型名查表 → 默认 1M）；裁剪预算 B = min(CLI `--max-context-tokens`, 70% × W)，窗口小预算收紧、窗口大不裸奔。
3. **会话重命名**：Ctrl+R 快捷键 + `/rename <title>` 斜杠命令，手动标题固定（不再被自动标题覆盖）。

## 2. 迭代约束（用户决定）

- **预算语义**：B = min(CLI 值, 70% × W)；W 是显示分母/展示上限，B 是裁剪真实上限。
- **数据来源**：真实 usage（`stream_options.include_usage`）为主，provider 不支持/报错自动回退启发式（标注 `~`）。
- **展示位置**：仅状态栏常驻（不做对话区 `[ctx]` 行）。
- **重命名策略**：手动命名后固定（pin），不再被自动标题覆盖；可再次 Ctrl+R 改名。
- **数据管道**：沿现有回调，`LLMResponse` 加 `usage`，`run_task` 加 `on_stats` 回调（方案 A，不加共享 StatsTracker）。

## 3. 范围

**In scope**
- `llm.py`：`resolve_context_window()`（/models → 查表 → 默认 1M，get_json 可注入）；`Usage` dataclass；`LLMResponse.usage`；流式请求带 `stream_options.include_usage`（4xx 被拒时去掉重试一次）；末 chunk usage 解析。
- `agent.py`：`AgentSession` 新增 `context_window` 属性与预算计算；`run_task` 新增 `on_stats` 回调；启发式回退构造 Usage；`rename_session(title)`；标题 pin 透传。
- `session.py`：`SessionStore.rename(session_id, title)`（pin 标记）；`save()` 尊重 pin。
- `cli.py`：`--context-window <n>` 覆盖自动解析；`handle_command` 新增 `/rename <title>`。
- `tui/widgets.py`：StatusBar 渲染 `ctx x/B 占比` 与 cache 段（启发式 `~`、无缓存数据省略、窄宽度省略百分比）。
- `tui/app.py`：Ctrl+R rename 模式（输入栏），提交重命名 + 刷新状态栏/会话列表；Esc 取消。
- `tui/worker.py`：桥接 `on_stats`。
- 测试、文档同步、ADR-023。

**Out of scope（本期不做）**
- 对话区 `[ctx]` 统计行；子智能体单独 stats 展示（其消耗并入父级回合）。
- 精确计费、token 累计统计、历史峰值。
- 多轮缓存命中率趋势图。
- `/models` 的持久缓存 / 配置持久化。

## 4. 上下文窗口解析与预算

### 4.1 `resolve_context_window(model, base_url, api_key, *, get_json=None, default=1_000_000) -> int`

解析顺序（任一命中即返回，失败静默走下一级，永不抛）：
1. `GET {base}/models`，匹配 `id == model`，读 `context_length` 字段（OpenAI/DeepSeek 惯例）；
2. 模型名查表（兜底）：`gpt-4o/gpt-4o-mini/gpt-4.1* = 128_000`、`o1/o3* = 200_000`、`deepseek-chat/deepseek-reasoner = 64_000`；
3. 默认 **1_000_000**（1M）。

`get_json` 可注入（离线测试 mock `/models`）；网络失败 / 非 JSON / 无匹配条目 → 走下一级。

### 4.2 预算 B

```
B = min(CLI --max-context-tokens 值, int(0.7 × W))
```

- `AgentSession.__init__` 计算并存 `self.context_window` / `self.max_context_tokens`（B）。
- 效果：W=64K → B≈44.8K（收紧防 API 拒）；W=128K → B=90K（默认值，不变）；W=1M → B=90K（不变）。
- CLI `--context-window <n>` 覆盖自动解析；`--max-context-tokens` 始终是硬上限。

## 5. usage 捕获（llm.py）

### 5.1 请求侧

- `chat()` 流式请求默认带 `stream_options: {"include_usage": true}`。
- **严格网关兜底**：`LLMClient` 加 `use_usage: bool = True`；首次请求若因该字段被拒（HTTP 4xx → LLMError），自动以 `use_usage=False`（不带该字段）重试一次，再失败走既有错误路径。
- 未启用流式/非 SSE 场景不涉及（本项目恒流式）。

### 5.2 解析侧

- `_StreamAccumulator` 捕获末 chunk（`choices` 为空且含 `usage`）的原始 usage。
- `LLMResponse.usage: dict | None`（原始 usage，便于扩展）。
- `Usage` dataclass：`prompt_tokens / completion_tokens / total_tokens / cached_tokens`（`prompt_tokens_details.cached_tokens`，缺省 0）。
- 命中率 = `cached_tokens / prompt_tokens`，在展示层计算（llm 层不含展示逻辑）。

### 5.3 启发式回退

- `response.usage is None` 时，agent 用 `estimate_tokens(sum(build_messages(B) 内容))` 构造 `Usage`，标记 `heuristic=True`；展示层显示 `~` 前缀。

## 6. agent + TUI 数据流（方案 A）

```
agent.run_task
  每回合 llm.chat 后 → on_stats(stats: Usage) 回调
    → AgentWorker._stats → app.call_from_thread
      → CodeAgentApp._on_stats → StatusBar.update_status(... ctx/cache ...)
  AgentSession 暴露 context_window / last_usage（含 heuristic 标记）
```

- `run_task` 新增 `on_stats(usage)` 回调（每回合 LLM 调用后触发）。
- 子智能体不接 stats 回调：其消耗计入父级该回合，不单独展示（父级回合 prompt_tokens 已含子会话消息）。
- 启发式回退时 `last_usage.heuristic=True`。

### 6.1 StatusBar 展示格式

```
… | ctx 12.4k/90k 14% cache 43% | ● idle
```

- 分子 = 最近回合 `prompt_tokens`；分母 = **预算 B**（与裁剪行为一致，非 W）。
- 展示条件与 cache 段：
  - usage 存在（真实）：`12.4k/90k 14%`；`cached_tokens>0` → 追加 `cache N%`；`cached_tokens==0` → 不显示 cache 段。
  - usage 为 None（启发式回退）：`~12.4k/90k 14%`，无 cache 段。
  - 既无 usage 也无启发式值（未跑任务）：不显示 ctx 段。
- 宽度不足时省略百分比（保留 `x/B`）。
- 若 x > B（启发式估算偏差等），百分比段加 `!` 告警。

## 7. 会话重命名（SessionStore / AgentSession / CLI / TUI）

### 7.1 SessionStore

- 新增 `rename(session_id, title) -> None`：读 meta → 置 `title` + `title_pinned: true` → 保留 `created_at`、更新 `updated_at` → 原子重写整个文件（首行 meta 替换，其余行原样；复用 `_write`）。meta 缺失（会话不存在）→ 抛 `KeyError(session_id)`（与 `load` 一致）。
- `save()` 尊重 pin：existing pinned → 忽略传入 title 保持现名；否则用传入 title（未 pinned 仍每次自动标题更新）。

### 7.2 AgentSession

- 新增 `rename_session(title) -> str`：
  - `session_id is None`（从未跑过任务）→ 先 `store.create(title)` 建会话并置 `session_id`，再 `store.rename` 固定；
  - 否则直接 `store.rename(session_id, title)`。
  - 返回最终 title。
- `_title()` 自动标题逻辑不变（未 pinned 时每次保存更新）。

### 7.3 CLI

- `handle_command` 新增 `/rename <title>`：调用 `session.rename_session`；空标题 → 输出用法提示（`/rename <title>`）；成功 → 输出 `renamed: <title>`。text 模式与 TUI 共用。

### 7.4 TUI

- `Binding("ctrl+r", "rename_session", "Rename")`。
- `action_rename_session`：进入 rename 模式（`PromptInput` 加 `rename` 状态类，占位符 `❯ 输入新会话名（回车确认，Esc 取消）`）。
- 提交 → `session.rename_session` → 刷新 StatusBar + SessionList + notify `renamed: <title>`。
- Esc 取消退出 rename 模式（纯前端状态，无 worker 阻塞）。

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| `/models` 请求失败 / 非 JSON / 无匹配 | 静默走查表 / 默认 1M |
| provider 拒 `include_usage`（4xx） | 去字段重试一次，再失败走既有错误路径 |
| provider 不回 usage | 启发式回退，`~` 标注 |
| 展示宽度不足 | 省略百分比，保留 `x/B` |
| `rename` 目标会话不存在（KeyError） | 提示 `session not found: <id>`，不改状态 |
| `/rename` 无标题 | 输出用法提示 |

## 9. 测试计划（全部离线）

- `test_llm_parse.py`：
  1. 末 chunk usage 解析（prompt/completion/total/cached 正确）。
  2. usage 缺失（无末 chunk usage）→ `LLMResponse.usage is None`。
  3. 缺 `prompt_tokens_details.cached_tokens` → `cached_tokens=0`。
  4. `include_usage` 首次 4xx 被拒 → 去掉字段重试成功。
  5. `resolve_context_window`（mock `get_json`）：/models 命中 / 查表回退 / 默认 1M / 网络失败静默。
- `test_agent.py`：
  6. 预算 B = min(CLI, 0.7×W)：W=64K→44.8K、W=128K→90K、W=1M→90K。
  7. `on_stats` 每回合触发（含 `heuristic=True` 回退标记）。
  8. `rename_session`：无 id 先建后 pin；有 id 直接 rename。
- `test_session.py`：
  9. `rename` 后 `save` 不再覆盖标题（pin 生效）。
  10. `rename` 保留 created_at / 更新 updated_at / 消息行原样保留。
  11. `rename` 不存在会话 → KeyError。
- `test_cli.py`：
  12. `/rename <title>` 成功输出 `renamed:`；空标题输出用法提示。
- `test_tui_widgets.py`：
  13. StatusBar 渲染 `ctx x/B 占比`；cache 段有/无；启发式 `~`；窄宽度省略百分比。
- `test_tui_app.py`：
  14. Ctrl+R 进入 rename 模式 → 提交重命名 → 状态栏/会话列表刷新。
  15. Esc 取消退出 rename 模式。
- 回归：全量 `uv run pytest` 全绿 + `uv run python -m code_agent --help` 正常 + 凭据 grep 复核。

## 10. 文档同步

- `docs/architecture.md`：llm.py（`Usage`/`resolve_context_window`/`LLMResponse.usage`/`use_usage`）、agent.py（`context_window`/`on_stats`/`rename_session`）、session.py（`rename`/pin）、tui 状态栏与 Ctrl+R、CLI `--context-window`/`/rename`。
- `docs/context-management.md`：预算 B 语义（min(CLI, 70%×W)）。
- `docs/development.md`：CLI 参数、TUI 快捷键 Ctrl+R、测试目录说明。
- `code_agent/docs/superpowers/specs/2026-08-31-observability-rename-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-023（不入库，ADR-007）。

## 11. 开发顺序（小步推进，每步可验证）

1. `session.py` rename + pin + `test_session.py`（TDD）
2. `llm.py` Usage / include_usage / usage 解析 / `resolve_context_window` + `test_llm_parse.py`（TDD）
3. `agent.py` 预算计算 / `context_window` / `on_stats` / 启发式回退 / `rename_session` + `test_agent.py`（TDD）
4. `cli.py` `--context-window` + `/rename` + `test_cli.py`（TDD）
5. `tui/` StatusBar 渲染 + Ctrl+R rename 模式 + `on_stats` 桥 + `test_tui_*.py`（TDD）
6. 文档同步 + ADR-023
7. 全量回归 + 凭据复核 + 提交
