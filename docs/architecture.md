# 架构说明

> 描述模块划分、职责边界与数据流，随实现逐步完善。

## 1. 模块总览

| 模块 | 职责 | 依赖 |
|---|---|---|
| `cli.py` | 命令行入口，解析参数，启动会话，打印流式输出 | agent, context |
| `agent.py` | 会话循环：组织往返、解析输出、执行工具、判定终止、错误恢复 | llm, tools, context |
| `context.py` | 维护消息序列、token 估算、预算裁剪、tool 结果处理 | 无（纯逻辑） |
| `tools.py` | 工具 schema 定义 + 本地执行器 + 结果格式化 | 无（纯逻辑，标准库） |
| `llm.py` | OpenAI 兼容 API 调用（流式）、响应/工具调用解析、重试 | httpx/requests |

## 2. 数据流

```
cli（用户任务）
  │
  ▼
agent.AgentSession.initialize(system_prompt, user_task)
  │  通过 context.add_user_message(...) 加入消息
  │
  ┌─────────────────────────────────────────────┐
  ▼                                             │
loop:                                          │
  messages = context.build_messages()           │  ← 含裁剪后的历史
  llm.chat(messages, tools=tools.schemas())     │
  stream 输出 → cli 展示                         │
  parse(response):                              │
    if tool_calls:                              │
      for each call:                            │
        result = tools.execute(call)            │  ← 本地执行
        context.add_tool_result(...)            │
      continue → next loop                     │
    else:                                       │
      final answer → 终止                       │
  if 终止条件: 终止                             │
  └─────────────────────────────────────────────┘
```

## 3. 模块接口约定

### llm.py
- `LLMClient(base_url, api_key, model)` — 从环境变量读取配置。
- `chat(messages, tools=None, stream=True) -> Iterator[Delta]` — 流式返回增量。
- `LLMResponse`：解析结果，含 `content: str` 与 `tool_calls: list[ToolCall]`。
- `ToolCall`：`{id, name, arguments(dict)}`。
- 内部：指数退避重试（默认 3 次）、超时、响应中 tool_calls 的健壮解析。

### tools.py
- `TOOLS: list[ToolSchema]` — 全部工具 JSON Schema。
- `execute(name, arguments, workdir) -> ToolResult`。
- `ToolResult`：`{ok: bool, output: str, truncated: bool, exit_code?: int}`。
- 所有输出为纯文本，便于回填给模型；超长自动截断。

### context.py
- `Conversation` 类：内部维护消息列表（system/user/assistant/tool）。
- `add_*()` 各类型消息入口。
- `build_messages(max_tokens) -> list` — 估算 token，超限则裁剪中间轮次。
- `estimate_tokens(text) -> int` — 粗略估算（可基于字符数/分词器，见 `context-management.md`）。

### agent.py
- `AgentSession(prompt, workdir, max_iterations, ...)`。
- `run() -> RunResult` — 主循环，`RunResult` 含最终答复、迭代数、是否正常结束。
- 终止条件：正常结束（无 tool_call）／达到 `max_iterations`／连续失败超阈值。
- 错误恢复：工具异常包装后回传模型；解析失败生成修复提示。

## 4. 设计原则

- 每层只依赖相邻层的接口，可独立单元测试。
- 工具执行器是纯本地逻辑，绝不调用任何服务端托管工具。
- 输出均为流式文本，CLI 与 agent 解耦。
- 变更模块内部实现时，不破坏对外接口约定。
