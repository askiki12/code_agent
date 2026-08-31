# 上下文与消息管理

> 描述对话历史维护、token 预算与裁剪策略，随实现逐步完善。

## 1. 目标

在不超模型上下文窗口的前提下，尽量保留对当前任务最相关的信息，保证 agent 在长任务中的稳定性。

## 2. 消息结构

`context.py` 内的 `Conversation` 维护有序消息列表，角色包括：

| role | 来源 |
|---|---|
| `system` | 会话初始化时的系统提示（不可裁剪，始终保留） |
| `user` | 用户任务 / 任务后续补充 |
| `assistant` | 模型输出（含 tool_calls 时保留 arguments 文本） |
| `tool` | 工具执行结果（通过 `tool_call_id` 与 assistant 的调用对应） |

## 3. token 估算

- 目标：低成本、可用的估算。实现时优先：
  1. 若安装了模型对应分词器（如 `tiktoken`），用它做精确估算（可选依赖）；
  2. 否则用启发式：中文约 1 字 ≈ 1~2 token，英文约 4 字符 ≈ 1 token。
- `estimate_tokens(text)` 作为统一入口，方便日后替换精确实现。

## 4. 预算与裁剪策略

- 配置 `max_context_tokens`（默认 90000；实际预算 **B = min(CLI `--max-context-tokens`, int(0.7 × W))**，W 为上下文窗口，如 128K 窗口 → 70% ≈ 90K）。
- 上下文窗口 W 由 `resolve_context_window` 解析：/models `context_length` → 模型名查表 → 默认 1M（永不抛）；CLI `--context-window <n>` 可覆盖。
- TUI 状态栏以 B 为分母展示最近回合 `prompt_tokens` 占用（占比）与缓存命中率：真实 usage 优先，缺失时启发式回退并加 `~` 前缀。
- 每次 `build_messages()` 时估算总量；若超限，按以下顺序裁剪：
  1. **始终保留**：`system` + 最近 N 轮完整往返（确保最新决策上下文完整）；
  2. 裁剪中间轮次：从最老的非 system 消息开始丢弃（成对丢弃 assistant+tool，保持结构完整）；
  3. 若仍超限，对最老的剩余消息做截断（内容超长裁剪），并可在 system 中注入"上下文已压缩"提示。
- 裁剪以“assistant tool_call + 其后 tool 消息”为组整体进行，从结构上杜绝悬空的 `tool` 消息（无对应 assistant tool_call）；`Conversation.is_valid()` 可用于校验与测试。

## 5. 工具结果处理

- 单个工具输出超长（> 上限，见 `tools.md`）：在 `tools.py` 截断并标记 `truncated`。
- 截断后再入 `Conversation`，防止单条消息撑爆上下文。

## 6. 一致性校验规则

- `tool` 消息必须有对应的 `tool_call_id`，且前面存在带该 id 的 assistant tool_call。
- 若校验失败，视为内部错误：丢弃该 tool 消息并记录警告（不静默继续）。

## 7. 未来扩展（暂不实现）

- 中间轮次的语义摘要（用模型压缩历史）——显著增强长任务能力，作为候选增强。
- 精确 token 计费统计（当前仅做裁剪预算，不对外计费）。

## 8. 会话持久化

- 对话可通过 `Conversation.to_jsonl()` / `from_jsonl()` 序列化到 JSONL（逐行一条消息）。
- 存储由 `session.SessionStore` 管理：`<workdir>/.code_agent/sessions/<id>.jsonl`，首行 meta。
- `AgentSession` 每次 `run_task` 结束自动保存；`--resume <id>` / 交互 `/resume` 恢复会话（重新注入当前 system prompt）。
- `.code_agent` 为受保护路径，工具层不可读写。
