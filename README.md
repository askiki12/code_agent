# code_agent

自研编程智能体（coding agent）：通过大语言模型自主读写文件、执行命令完成编程任务。

## 运行

```bash
# 环境（uv，隔离且可复现）
uv sync

# 配置凭据：复制模板为 .env 并填入真实值（.env 已 gitignore，不会入库）
cp .env.example .env
#   编辑 .env：CODE_AGENT_BASE_URL / CODE_AGENT_API_KEY / CODE_AGENT_MODEL
#   或用系统环境变量（优先级更高）：export CODE_AGENT_API_KEY=sk-...

# 一次性任务
uv run python -m code_agent --prompt "把 tests/test_tools.py 里的测试全部跑通"

# 交互式
uv run python -m code_agent --interactive

# 测试
uv run pytest tests/ -v
```

## 功能

- 5 个本地工具：read_file / write_file / edit_file / list_dir / run_command
- 流式输出、上下文 token 预算与自动裁剪、错误恢复与重试
- 受保护路径（.env / .git）禁读禁写；命令超时与输出截断

详见 `docs/`。
