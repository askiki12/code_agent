# code_agent 设计文档

> 本文档描述项目的目标、约束、总体设计与开发路线，随开发进度逐步完善。

## 1. 项目目标

个人独立设计并实现一个编程智能体（coding agent）：通过与大语言模型交互，自主读写文件、执行命令，完成交给它的编程任务。形态类似一个简化的 Claude Code / Codex / OpenCode。

## 2. 硬性约束（来自题目）

- **禁止**使用任何 agent 框架 / SDK（LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等）。
- **允许**使用模型厂商 API 客户端库、OpenAI 兼容网关及模型原生 tool calling 接口。
- **禁止**依赖 API 服务端托管的代码执行或文件工具（如 Code Interpreter、Files API）。
- 重要逻辑全部自行实现：对话历史与上下文管理、工具定义与本地执行、模型输出解析、循环终止条件、错误处理。
- API key 一律通过环境变量或未入库的配置文件提供，绝不进入仓库。
- 截止时间：2026-09-02 24:00（北京时间）。
- 提交物：公开 Git 仓库 + README.txt（1000 汉字内）+ 演示视频（2 分钟内，mp4 ≤ 200MB）。

## 3. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 生态全、模型 SDK 支持好、异步/HTTP 简单 |
| 模型接入 | OpenAI 兼容接口（/chat/completions） | 一个 Provider 抽象，切换模型只改环境变量 |
| HTTP | requests 或 httpx | 依赖尽量少 |
| 配置 | 环境变量 / `.env` 自动加载（`.env` 不入库） | 满足凭据要求 |
| 环境管理 | uv（`.venv/` + `uv.lock`） | 环境隔离、可复现、`uv sync` 一键重建 |

## 4. 总体架构（方案 B：分层模块）

明确分层，每层可独立测试：

```
code_agent/
├── code_agent/
│   ├── cli.py       # 命令行入口（一次性 / 交互式）
│   ├── agent.py     # 会话循环、终止条件、错误恢复
│   ├── context.py   # 上下文/消息管理、token 预算、裁剪
│   ├── tools.py     # 工具注册表 + 本地执行器
│   └── llm.py       # OpenAI 兼容 API 封装（流式 + tool calling）
└── tests/           # 单元测试
```

## 5. 核心数据流（agent 循环）

```
用户任务
  → 初始化会话（system prompt + 首条用户消息）
  → 循环：
      ① 组装消息 → 调用模型
      ② 解析输出：
         - 有 tool_calls → 本地执行对应工具 → 结果以 role=tool 回填 → 继续
         - 无 tool_calls  → 该输出即最终答复 → 终止
         - 命中终止条件  → 终止
```

## 6. 功能范围（核心功能优先）

v0.1.0 已实现范围：

- [x] 核心 agent 循环（调用模型 → 解析 → 执行工具 → 回填 → 判定终止）
- [x] 工具：read_file / write_file / edit_file / list_dir / run_command（全部自实现）
- [x] 上下文管理：消息序列维护、token 预算、超限裁剪、超长 tool 结果处理
- [x] 错误处理：工具错误回传、API 重试（指数退避）、命令超时、解析异常恢复、LLM 错误优雅停止
- [x] CLI：`--prompt` 一次性任务 + `--interactive` 对话模式，流式输出
- [x] 搜索工具 glob/grep（纯标准库自实现，grep 带基础 gitignore）
- [x] 测试：工具/解析/上下文单元测试 + mock 模型集成测试 + 真实 API 冒烟测试（79 用例全绿）

暂不实现（留作后续扩展，遵循 YAGNI）：

- 并行工具调用、会话 checkpoint 恢复、复杂 token 计费策略、Web 界面。

## 7. 关键机制

详见配套文档：

- `architecture.md`：模块划分与数据流细节
- `tools.md`：工具定义与约定（JSON Schema 源）
- `context-management.md`：消息历史 / token 预算 / 裁剪策略
- `development.md`：运行 / 测试 / 验证 / 演示流程

## 8. 开发路线（进度截至 2026-08-29）

1. [x] 搭建骨架：模块空壳 + CLI 入口 + 项目配置
2. [x] 实现 `llm.py`：OpenAI 兼容封装 + tool_calls 解析
3. [x] 实现 `tools.py`：五个工具的本地执行器
4. [x] 实现 `context.py`：消息维护 + token 预算 + 裁剪
5. [x] 实现 `agent.py`：循环、终止条件、错误恢复
6. [x] 测试与冒烟：单元测试 → mock 集成测试 → 真实 API（已完成，79 用例 + 三次真实冒烟）
7. [x] 迭代增强：新增 glob/grep 搜索工具（ADR-011，设计见 docs/superpowers/specs/2026-08-29-glob-grep-design.md）
8. [ ] 演示准备：README.txt + 演示任务 + 视频脚本（待办）
