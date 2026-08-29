# 架构说明

> 描述模块划分、职责边界与数据流，随实现逐步完善。

## 1. 模块总览

| 模块 | 职责 | 依赖 |
|---|---|---|
| `cli.py` | 命令行入口，解析参数，加载 `.env`，启动会话，打印流式输出 | agent, llm |
| `agent.py` | 会话循环：组织往返、解析输出、执行工具、判定终止、错误恢复 | llm, tools, context |
| `context.py` | 维护消息序列、token 估算、预算裁剪、tool 结果处理 | 无（纯逻辑） |
| `tools.py` | 工具 schema 定义 + 本地执行器 + 结果格式化（含 glob/grep 搜索与 gitignore 过滤） | 无（纯逻辑，标准库） |
| `session.py` | 会话持久化：SessionStore（JSONL 存储/列表/恢复） | 无（纯逻辑，标准库） |
| `llm.py` | OpenAI 兼容 API 调用（流式）、响应/工具调用解析、重试 | requests |

## 2. 数据流

```
cli（用户任务）
  │  main() 先 _load_dotenv() 再构造 AgentSession
  │  （cli 通过 _make_store(workdir) 构造 SessionStore，AgentSession 每轮 run_task 结束自动保存会话）
  ▼
agent.AgentSession.run_task(task)
  │  通过 conversation.add_user(task) 加入消息
  │
  ┌────────────────────────────────────────────────┐
  ▼                                                │
loop:                                             │
  messages = conversation.build_messages(max_tokens)│  ← 含裁剪后的历史
  response = llm.chat(messages, tools=TOOL_SCHEMAS) │  ← 流式，on_delta 实时展示
  conversation.add_assistant(content, tool_calls)   │
  if not response.tool_calls:                       │
      final answer → 终止                          │
  for tc in response.tool_calls:                    │
      result = tools.execute(tc.name, tc.arguments) │  ← 本地执行
      conversation.add_tool(tc.id, tc.name, out)    │
  continue → next loop                             │
  if 终止条件: 终止                                 │
  └────────────────────────────────────────────────┘
```

## 3. 模块接口约定（与实际实现一致）

### llm.py
- `LLMClient(*, base_url, api_key, model, timeout=300.0, max_retries=3, debug=False)` — 配置来自环境变量 / `.env` / 命令行。
- `chat(messages, tools=None, on_delta=None) -> LLMResponse` — 流式 SSE，`on_delta` 按增量回调展示。
- `LLMResponse`：`content: str` 与 `tool_calls: list[ToolCall]`。
- `ToolCall`：`{id: str, name: str, arguments: dict}`。
- 错误语义：429/5xx/网络错误指数退避重试（默认 3 次）；非重试性 HTTP 错误与畸形 tool 参数 JSON 抛 `LLMError`。

### tools.py
- `TOOL_SCHEMAS: list[dict]` — 7 个工具（read_file/write_file/edit_file/list_dir/run_command/glob/grep）的 OpenAI functions JSON Schema。
- `execute(name, args, workdir) -> ToolResult`。
- `ToolResult`：`{ok: bool, output: str, truncated: bool, exit_code?: int}` + `as_message()`。
- 所有输出为纯文本，便于回填给模型；超长自动截断（默认 8000 字符）；受保护路径（`.env*` 除 `.env.example`、`.git`）禁读禁写；写操作限定工作目录内。

### session.py
- `SessionStore(root)` — root 为 `<workdir>/.code_agent/sessions`。
- `list_sessions() -> list[dict]`（按 updated_at 倒序，含 message_count）/ `create(title) -> session_id` / `save(session_id, messages, title=None)`（全量原子写）/ `load(session_id) -> (meta, messages)`（缺失抛 KeyError，坏行跳过）。

### context.py
- `Conversation` 类：内部维护消息列表（system/user/assistant/tool）。
- `add_system` / `add_user` / `add_assistant(content, tool_calls=None)` / `add_tool(tool_call_id, name, output)`。
- `messages`（返回拷贝）、`is_valid()`（结构一致性校验）。
- `build_messages(max_tokens) -> list` — 估算 token，超限按组裁剪（assistant+其后 tool 成组，不产生孤儿 tool）。
- `estimate_tokens(text) -> int` — 启发式估算（CJK≈1 token，其它≈每 4 字符 1 token）。

### agent.py
- `AgentSession(*, workdir, llm, max_iterations=20, max_context_tokens=90000, debug=False)` — `llm` 依赖注入，便于测试。
- `run_task(task, on_delta=None) -> RunResult` — 主循环；`RunResult` 含 `final_text/iterations/finished/reason`。
- 终止条件（三条）：无 tool_calls（`complete`）／达到 `max_iterations`／连续失败（工具或 LLM 错误）达 3 次。
- 错误恢复：工具异常包装为 `ToolResult(ok=False)` 回传模型；`LLMError` 注入修复提示并计数，达阈值优雅终止。

## 4. 设计原则

- 每层只依赖相邻层的接口，可独立单元测试。
- 工具执行器是纯本地逻辑，绝不调用任何服务端托管工具。
- 输出均为流式文本，CLI 与 agent 解耦。
- 变更模块内部实现时，不破坏对外接口约定。
