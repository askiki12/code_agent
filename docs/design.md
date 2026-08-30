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
- [x] 测试：工具/解析/上下文单元测试 + mock 模型集成测试 + 真实 API 冒烟测试（272 用例全绿）
- [x] 会话持久化与多会话管理（JSONL 存储，--list-sessions/--resume，交互斜杠命令 /new /list /resume）
- [x] 工作区一等公民（workspace.json 元数据，交互启动展示概况与上次会话续接提示）
- [x] 权限模型（allow/ask/deny 三态 + 只读命令白名单 + doom_loop 重复检测，--allow/--deny/--ask）
- [x] skill 机制（SKILL.md 技能库，use_skill 按需加载，项目+用户级）
- [x] web_fetch 网络检索工具（公网校验 SSRF + 文本提取，ADR-016）
- [x] web_search 关键词搜索（DDG Lite 免 key，ADR-017）
- [x] 子智能体派遣（dispatch_subagent，同步嵌套循环，子智能体阉割派遣，ADR-018）
- [x] TUI 终端界面（rich，--interactive 升级，状态栏+对话区+输入栏，ADR-019）
- [x] Textual TUI 重构（可滚动对话+会话列表+快捷键，ADR-020）
- [x] TUI 打磨（多轮渲染修复、Ctrl+L 布局修复、子智能体运行标注、! 命令、移除命令面板，ADR-021）
- [x] TUI 收尾（! 命令模式、Ctrl+P 隐藏、Ctrl+S 技能弹窗、skill 加载标注，ADR-022）

暂不实现（留作后续扩展，遵循 YAGNI）：

- 并行工具调用、会话 checkpoint 恢复、复杂 token 计费策略、Web 界面。

## 7. 关键机制

详见配套文档：

- `architecture.md`：模块划分与数据流细节
- `tools.md`：工具定义与约定（JSON Schema 源）
- `context-management.md`：消息历史 / token 预算 / 裁剪策略
- `development.md`：运行 / 测试 / 验证 / 演示流程

## 8. 开发路线（进度截至 2026-08-30）

1. [x] 搭建骨架：模块空壳 + CLI 入口 + 项目配置
2. [x] 实现 `llm.py`：OpenAI 兼容封装 + tool_calls 解析
3. [x] 实现 `tools.py`：五个工具的本地执行器
4. [x] 实现 `context.py`：消息维护 + token 预算 + 裁剪
5. [x] 实现 `agent.py`：循环、终止条件、错误恢复
6. [x] 测试与冒烟：单元测试 → mock 集成测试 → 真实 API（已完成，272 用例 + 三次真实冒烟）
7. [x] 迭代增强：新增 glob/grep 搜索工具（ADR-011，设计见 docs/superpowers/specs/2026-08-29-glob-grep-design.md）
8. [ ] 演示准备：README.txt + 演示任务 + 视频脚本（待办）
9. [x] 迭代增强：会话持久化 + 多会话管理（ADR-012，设计见 docs/superpowers/specs/2026-08-29-session-persistence-design.md）
10. [x] 迭代增强：工作区一等公民（ADR-013，设计见 docs/superpowers/specs/2026-08-29-workspace-design.md）
11. [x] 迭代增强：权限模型（ADR-014，设计见 docs/superpowers/specs/2026-08-29-permissions-design.md）
12. [x] 迭代增强：skill 机制（ADR-015，设计见 docs/superpowers/specs/2026-08-29-skills-design.md）
13. [x] 迭代增强：web_fetch 网络检索（ADR-016，设计见 docs/superpowers/specs/2026-08-30-web-fetch-design.md）
14. [x] 迭代增强：web_search 关键词搜索（ADR-017，设计见 docs/superpowers/specs/2026-08-30-web-search-design.md）
15. [x] 迭代增强：子智能体派遣（ADR-018，设计见 docs/superpowers/specs/2026-08-30-subagent-design.md）
16. [x] 迭代增强：TUI 终端界面（ADR-019，设计见 docs/superpowers/specs/2026-08-30-tui-design.md）
17. [x] 迭代增强：Textual TUI 重构（ADR-020，设计见 docs/superpowers/specs/2026-08-30-tui-textual-design.md）
18. [x] 迭代增强：TUI 打磨（ADR-021，设计见 docs/superpowers/specs/2026-08-30-tui-polish-design.md）
19. [x] 迭代增强：TUI 收尾（ADR-022，设计见 docs/superpowers/specs/2026-08-30-tui-final-design.md）
