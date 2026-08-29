# code_agent

自研编程智能体（coding agent）：通过大语言模型自主读写文件、执行命令完成编程任务。

## 运行

```bash
export CODE_AGENT_BASE_URL="https://api.example.com/v1"
export CODE_AGENT_API_KEY="sk-..."   # 仅环境变量，切勿入库
export CODE_AGENT_MODEL="gpt-4o-mini"

# 一次性任务
python -m code_agent --prompt "把 tests/test_tools.py 里的测试全部跑通"

# 交互式
python -m code_agent --interactive
```

## 功能

- 5 个本地工具：read_file / write_file / edit_file / list_dir / run_command
- 流式输出、上下文 token 预算与自动裁剪、错误恢复与重试
- 受保护路径（.env / .git）禁读禁写；命令超时与输出截断

详见 `docs/`。
