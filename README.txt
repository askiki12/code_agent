自研编程智能体 code_agent
仓库：https://github.com/askiki12/code_agent.git

类 Claude Code/Codex 的编码智能体：LLM 驱动，自主读改文件、跑命令完成编程任务。核心逻辑全部自实现，零 agent 框架、零服务端工具。

运行（uv + OpenAI 兼容 API，凭据存 .env 不入库）：
uv sync
uv run python -m code_agent --prompt "跑通全部测试"
uv run python -m code_agent -i   # Textual TUI
uv run pytest tests/ -q          # 379 个离线测试

功能：本地工具自实现，模型可见 10~14 个按条件注入——read_file/write_file/edit_file(精确唯一替换)/run_command/glob/grep/web_fetch(SSRF 防护)/web_search；编排：dispatch_subagent(子智能体，harness 强制阉割递归)、use_skill、记忆 remember/recall/create_skill。会话持久化恢复、工作区、权限 deny/ask/allow+doom_loop、上下文成组裁剪(无孤儿 tool)、Textual TUI。
特色：agent 原生长期记忆——任务开始自动注入相关记忆、成功自动沉淀经验，可复用流程沉淀为 skill，跨会话"越用越聪明"。

工程：全程 TDD；15 轮迭代每轮 spec→计划→独立评审→人验收；27 条 ADR；180 次提交历史完整。设计模式：工具层 Command+Registry+策略装饰；主循环 Template Method+Observer 回调解耦；权限/模型接入 Strategy；deny→ask→allow 责任链；会话 Memento+Repository；依赖注入支撑测试近全离线。安全：凭据仅 .env；受保护路径禁读写；命令超时截断；SSRF 代理感知。
开发方式：AI 提方案→人拍板→人验收，设计责任在人。
