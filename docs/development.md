# 开发与验证流程

> 描述如何运行、测试、验证和演示本项目，随开发逐步完善。

## 1. 环境准备（使用 uv 管理）

本项目使用 **uv** 管理实验环境，保证环境隔离与可复现：

```bash
# 1. 安装 uv（若未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh        # 或 pip install uv

# 2. 在仓库根目录创建并同步环境（生成 .venv/ 与 uv.lock）
uv sync

# 3. 在环境中运行（uv run 会自动使用 .venv）
uv run python -m code_agent --help
```

- 环境位置：`code_agent/.venv/`（已 gitignore）。
- 可复现性：`uv.lock` 锁定精确版本，已入库；任何机器 `uv sync` 得到一致环境。
- 依赖声明：运行依赖在 `pyproject.toml` 的 `[project]`，测试依赖在 `[dependency-groups].dev`（pytest）。
- 新增依赖后：改 `pyproject.toml` → `uv sync`（自动更新 `uv.lock`）。
- 不要求系统 Python 预装 `requests`/`pytest`；环境由 uv 完全隔离。
- 配置 API（推荐 `.env`，已 gitignore，不会入库）：

```bash
# 复制模板并填入真实值（系统环境变量优先于 .env；命令行参数优先于两者）
cp .env.example .env
# 编辑 .env：
#   CODE_AGENT_BASE_URL=https://api.example.com/v1
#   CODE_AGENT_API_KEY=sk-...        # 真实 key，勿外泄
#   CODE_AGENT_MODEL=gpt-4o-mini     # 或 deepseek-chat 等
```

  也可以在 shell 中导出同名环境变量，效果相同（优先级更高）。

## 2. 运行方式

一次性任务：

```bash
uv run python -m code_agent --prompt "把这个目录里所有测试跑通"
```

交互式对话：

```bash
uv run python -m code_agent --interactive
```

- 交互模式启动会展示工作区概况：`Workspace: <name> (<id>) | sessions: <n> | last: <last_session_id>`，并提示 `Tip: /resume <last_session_id>` 续接上次会话。
- 工作区元数据存于 `<workdir>/.code_agent/workspace.json`（自动维护，勿手动编辑）。

全部参数（`uv run python -m code_agent --help` 查看）：

- `--prompt <task>`：一次性任务。
- `-i` / `--interactive`：交互式模式（同一会话保持上下文）。
- `--workdir <dir>`：agent 工作目录（默认当前目录）。
- `--model <model>` / `--base-url <url>` / `--api-key <key>`：覆盖环境变量 / `.env`。
- `--max-iterations <n>`：最大循环轮次（默认 20）。
- `--max-context-tokens <n>`：上下文 token 预算（默认 90000）。
- `--debug`：输出详细日志。
- `--list-sessions`：列出 `<workdir>/.code_agent/sessions/` 下的会话（id/标题/消息数/更新时间）。
- `--resume <id>`：恢复指定会话（可与 `--prompt`/`--interactive` 组合）。
- `--allow <tool:pattern>` / `--deny <tool:pattern>` / `--ask <tool:pattern>`：权限规则（可重复），如 `--deny "run_command:pytest *"`。
- 三态：deny 拒绝 → ask 询问（交互模式 y/N，一次性任务直接拒绝）→ allow 放行；内置只读命令白名单（ls/cat/git status 等）为预留快路径，仅在默认策略收紧时才有意义（当前默认放行使其惰性，`--deny`/`--ask` 显式规则优先于白名单）。
- 连续相同工具调用达 3 次自动拒绝（doom_loop），防止模型重复卡死。
- 交互模式斜杠命令：`/new`（新建）、`/list`（列出）、`/resume <id>`（恢复）、`/exit`（退出）。

## 3. 测试

- 框架：`pytest`（经 `uv run`）。
- 目录：`tests/`（当前 134 个用例，全部离线，无需 API key）。
  - `test_smoke.py`：包可导入、版本号。
  - `test_tools.py`：七个工具的本地执行用例（含 glob/grep）。
  - `test_llm_parse.py`：tool_calls 响应解析（含异常格式）。
  - `test_context.py`：消息维护、token 估算、裁剪后结构一致性。
  - `test_agent.py`：用 mock 模型跑通完整循环（含终止条件与错误恢复，不含真实 API）。
  - `test_cli.py`：`.env` 加载、缺 key 报错、一次性任务入口。
  - `test_session.py`：SessionStore 创建/保存/加载/列表/坏文件容错。
  - `test_workspace.py`：工作区初始化/幂等/损坏容错/touch_session。
  - `test_permissions.py`：规则解析/三态/只读白名单/doom_loop/交互询问。
- 运行全部测试：

```bash
uv run pytest tests/ -v
```

## 4. 验证清单（每阶段合并前）

- [ ] `uv run pytest tests/ -v` 全部通过
- [ ] `uv run python -m code_agent --help` 正常输出
- [ ] 一次真实 API 冒烟任务（如修改一个测试文件并跑通）
- [ ] 无真实凭据被写入任何提交的文件（用 `git grep -i "sk-"` 复核）

## 5. 提交规范

- 提交历史保留完整，不压缩、不改写（评分依据）。
- 每次提交包含有意义的 message，可关联到 `docs/design.md` 开发路线中的步骤。
- 截止时间 2026-09-02 24:00 后不再推送。

## 6. 演示准备

- 演示任务建议：一个**真实且可快速验证**的编程任务（如：修一个 bug 并跑通测试）。
- 视频脚本要点：
  1. 展示一次性任务输入与流式输出；
  2. 展示 agent 自主调用 read_file / edit_file / run_command；
  3. 最终用命令验证结果（如跑通测试）。
- 产出物：`README.txt`（≤1000 汉字）+ 演示 mp4（≤200MB）。

## 7. 已知风险与对策

| 风险 | 对策 |
|---|---|
| 模型 tool_calls 格式不标准 | `llm.py` 健壮解析 + `test_llm_parse.py` |
| 长任务上下文溢出 | 预算裁剪策略（见 `context-management.md`） |
| 命令工具卡死 | 超时机制 + 无 TTY |
| 误写真实 API key 进仓库 | 环境变量唯一来源 + 提交前 grep 复核 |
| 环境不可复现 | uv + `uv.lock` 锁定版本；`uv sync` 一键重建 |
