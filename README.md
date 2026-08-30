# code_agent

自研编程智能体（coding agent）：通过大语言模型（OpenAI 兼容接口 + 原生 tool calling）自主读写文件、执行命令，完成交给它的编程任务。**零 agent 框架依赖**，对话/上下文/工具/解析/终止等核心逻辑全部自实现。形态类似简化的 Claude Code / Codex / OpenCode。

## 环境依赖

- **uv**（>= 0.4）：环境与依赖管理；Python 版本由 uv 自动托管（>= 3.11），无需系统预装 Python 包。
- **运行时依赖**：仅 `requests>=2.31`。
- **开发依赖**：`pytest>=8`（声明于 `pyproject.toml` 的 `[dependency-groups].dev`）。
- **版本锁定**：`uv.lock`（已入库）——任何机器 `uv sync` 得到完全一致的环境。
- 无需 Docker / conda / 其它系统级依赖。

## 快速开始

```bash
# 1. 安装 uv（若未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 克隆并创建环境（生成 .venv/，安装由 uv.lock 锁定的依赖）
git clone git@github.com:askiki12/code_agent.git && cd code_agent
uv sync

# 3. 配置凭据（.env 已 gitignore，不会入库；系统环境变量优先级更高）
cp .env.example .env
#   编辑 .env 填入真实值：CODE_AGENT_BASE_URL / CODE_AGENT_API_KEY / CODE_AGENT_MODEL

# 4. 验证安装
uv run python -m code_agent --help
```

## 使用

```bash
# 一次性任务
uv run python -m code_agent --prompt "把 tests/ 里的测试全部跑通"

# 交互式对话（同一会话保持上下文）
uv run python -m code_agent --interactive
```

常用参数（完整列表见 `uv run python -m code_agent --help`）：

| 参数 | 说明 | 默认 |
|---|---|---|
| `--prompt` | 一次性任务 | — |
| `-i` / `--interactive` | 交互式模式 | — |
| `--workdir <dir>` | agent 工作目录 | 当前目录 |
| `--model` / `--base-url` / `--api-key` | 覆盖环境变量 / `.env` | env → 内置默认 |
| `--max-iterations <n>` | 最大循环轮次 | 20 |
| `--max-context-tokens <n>` | 上下文 token 预算 | 90000 |
| `--list-sessions` | 列出本工作区会话并退出 | — |
| `--resume <id>` | 恢复指定会话（可与 `--prompt`/`--interactive` 组合） | — |
| `--allow` / `--deny` / `--ask <tool:pattern>` | 权限规则（可重复），如 `--deny "run_command:pytest *"` | — |
| `--debug` | 输出调试日志 | off |

交互模式内置斜杠命令：`/new`（新建会话）、`/list`（列出）、`/resume <id>`（恢复）、`/exit`（退出）。

## 测试与验证

```bash
uv run pytest tests/ -v      # 全量测试（当前 206 个，全部离线，无需 API key）
```

## 功能特性

- **9 个自实现本地工具**：`read_file` / `write_file` / `edit_file`（精确替换 + 原子写）/ `list_dir` / `run_command` / `glob` / `grep`（三种输出模式 + 基础 gitignore）/ `web_fetch`（公网页面标题/正文/链接，SSRF 防护）/ `web_search`（DuckDuckGo Lite 免 key，返回标题/URL/摘要）
- **会话持久化与多会话管理**：对话存 `<workdir>/.code_agent/sessions/`，`--list-sessions` / `--resume` 跨重启续接，交互斜杠命令 `/new` `/list` `/resume`
- **工作区一等公民**：`workspace.json` 元数据（稳定 id），交互启动展示项目概况与上次会话续接提示
- **权限模型**：`--allow` / `--deny` / `--ask` 三态规则 + 只读命令白名单 + doom_loop 重复检测；ask 交互询问 y/N，一次性任务降级拒绝
- **skill 机制**：项目级 + 用户级 SKILL.md 技能库，`use_skill` 按需加载（如 `~/.code_agent/skills/`）
- 流式输出；上下文 token 预算与成组裁剪（不会产生孤儿 tool 消息）
- 错误恢复：工具错误回传模型继续、API 指数退避重试、命令超时、LLM 错误优雅停止（不崩溃）
- 安全：受保护路径（`.env` / `.git` / `.code_agent`）禁读禁写；命令超时与输出截断；凭据仅走环境变量 / `.env`

## 项目结构

```
code_agent/
├── code_agent/            # 主包
│   ├── cli.py             # 命令行入口（含 .env 自动加载、权限参数、会话/工作区接线）
│   ├── agent.py           # 会话循环、终止条件、错误恢复、权限检查、skill 注入
│   ├── context.py         # 消息管理、token 预算、裁剪、序列化
│   ├── tools.py           # 工具 schema + 本地执行器
│   ├── llm.py             # OpenAI 兼容流式客户端 + tool_calls 解析
│   ├── session.py         # 会话持久化（SessionStore，JSONL）
│   ├── workspace.py       # 工作区元数据（Workspace）
│   ├── permissions.py     # 权限模型（Policy：三态/白名单/doom_loop）
│   ├── skills.py          # 技能库（SkillRegistry）
│   └── web.py             # 网络检索：公网校验/文本提取/fetch/search
├── tests/                 # 206 个离线测试（pytest）
├── docs/                  # 设计/架构/工具/上下文/开发文档 —— 接手者必读
├── pyproject.toml         # 依赖与元数据声明
└── uv.lock                # 环境版本锁定
```

## 继续开发（接手指南）

1. 先读 `docs/development.md`（环境/运行/测试/验证/演示流程）与 `docs/design.md`（设计总览与开发路线）。
2. 改动工具定义：`docs/tools.md` 是权威源（JSON Schema 依据），改工具必须同步更新 `tests/test_tools.py`。
3. 上下文策略见 `docs/context-management.md`；模块接口约定见 `docs/architecture.md`。
4. 新增依赖：改 `pyproject.toml` → `uv sync`（自动更新 `uv.lock`）。
5. 提交规范：保留完整提交历史，不 rebase 不改写；截止 2026-09-02 24:00 后不再推送。
6. 凭据纪律：任何真实 key 不得进入仓库 / 文档 / 视频。
