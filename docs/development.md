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
- 依赖声明：运行依赖在 `pyproject.toml` 的 `[project]`（requests + rich + textual），测试依赖在 `[dependency-groups].dev`（pytest）。
- 新增依赖后：改 `pyproject.toml` → `uv sync`（自动更新 `uv.lock`）。
- 不要求系统 Python 预装 `requests`/`rich`/`textual`/`pytest`；环境由 uv 完全隔离。
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
- `-i` / `--interactive`：交互式模式（同一会话保持上下文）。TTY 下自动进入 **Textual 全屏 TUI**；`NO_TUI=1` 或非 TTY 回退纯文本 `input()` 循环。
- **项目记忆**：CLI 构造 `AgentSession` 时固定 `memory=True`，`--prompt` 与 `--interactive` 两种入口均开启——首任务自动注入 top-K（≤3 条）相关记忆，任务成功结束自动总结沉淀，模型可随时用 `remember`/`recall`/`create_skill` 读写记忆与沉淀技能（见 tools.md §3.11-3.13）。
- TUI 行为：顶部状态栏（`Workspace: <完整路径> | model: <model> | session: <会话名>` + 运行状态 `● idle`/`● running`，未运行任务时会话名显示 `new`）、底部 StatusFooter（与快捷键同排最右侧紧凑 stats：`213.0k(21%) cache:40%`，真实 usage 优先、启发式 `~` 前缀、无缓存隐藏 cache 段，pct 相对上下文窗口 W）、中部可滚动对话区（滚轮 / PageUp / PageDown，流式增量更新，近底自动跟随、向上可回看）、右侧会话列表面板（Ctrl+L 切换，选项点击恢复会话）、底部输入栏；快捷键 Ctrl+Q（退出）/ Ctrl+N（新建会话）/ Ctrl+L（会话列表）/ Ctrl+S（技能选择弹窗，↑↓ 选择 Enter 使用 Esc 退出）/ Ctrl+R（重命名会话：输入新会话名回车确认，Esc 取消）；运行中任务在后台线程执行（UI 始终响应）；切会话（Ctrl+L 恢复 / Ctrl+N 新建）后 stats 立即刷新为该会话估算值（load 用 `estimate_tokens` 设启发式 `last_usage`、new 清空）；`!cmd` 直接执行终端命令（输入以 `!` 开头输入框切换 command-mode 变 warning 色、占位符变 `❯ shell: 输入命令（回车执行）`，用户主动命令不走权限 policy，后台线程运行、120s 超时、busy 互斥，结果回显到对话区）；skill 加载标注 `[skill] 加载 <name>… / ✓ / ✗`；子智能体运行标注 `[subagent] 子智能体运行中… / ✓ 完成`；工具行与 `[agent] stopped`/`[session ...]` 行色彩分层；权限 ask 在输入栏就地确认（y/N）。
- `--workdir <dir>`：agent 工作目录（默认当前目录）。
- `--model <model>` / `--base-url <url>` / `--api-key <key>`：覆盖环境变量 / `.env`。
- `--max-iterations <n>`：最大循环轮次（默认 20）。
- `--max-context-tokens <n>`：上下文 token 预算 CLI 值（默认 90000；实际预算 B = min(此值, 70% × 上下文窗口 W)）。
- `--context-window <n>`：模型上下文窗口 W（默认自动解析：/models `context_length` → 模型名查表 → 1M）。
- `--debug`：输出详细日志。
- `--list-sessions`：列出 `<workdir>/.code_agent/sessions/` 下的会话（id/标题/消息数/更新时间）。
- `--resume <id>`：恢复指定会话（可与 `--prompt`/`--interactive` 组合）。
- `--allow <tool:pattern>` / `--deny <tool:pattern>` / `--ask <tool:pattern>`：权限规则（可重复），如 `--deny "run_command:pytest *"`。
- 三态：deny 拒绝 → ask 询问（交互模式 y/N，一次性任务直接拒绝）→ allow 放行；内置只读命令白名单（ls/cat/git status 等）为预留快路径，仅在默认策略收紧时才有意义（当前默认放行使其惰性，`--deny`/`--ask` 显式规则优先于白名单）。
- 连续相同工具调用达 3 次自动拒绝（doom_loop），防止模型重复卡死。
- 交互模式斜杠命令：`/new`（新建）、`/list`（列出）、`/resume <id>`（恢复）、`/rename <title>`（重命名并 pin 固定标题）、`/exit`（退出）。
- 技能（skill）：从 `<workdir>/.code_agent/skills/<name>/SKILL.md`（项目级）与 `~/.code_agent/skills/<name>/SKILL.md`（用户级，同名项目优先）扫描。
  SKILL.md 格式：`---` frontmatter（`name` / `description`）+ markdown 正文；agent 通过 `use_skill` 工具按需加载并遵循执行。

## 3. 测试

- 框架：`pytest`（经 `uv run`）。
- 目录：`tests/`（当前 366 个用例，全部离线，无需 API key）。
  - `test_smoke.py`：包可导入、版本号。
  - `test_tools.py`：九个工具的本地执行用例（含 glob/grep）+ ToolRegistry/Command 用例（register/get、schemas 可见性过滤、execute 统一分派、validate 钩子、unknown tool）。
  - `test_llm_parse.py`：tool_calls 响应解析（含异常格式）、usage 解析（include_usage 兜底回退）、`resolve_context_window`（/models/查表/默认）。
  - `test_context.py`：消息维护、token 估算、裁剪后结构一致性。
  - `test_agent.py`：用 mock 模型跑通完整循环（含终止条件与错误恢复、dispatch_subagent 子智能体派遣/阉割双层强制/权限继承、上下文预算 B、on_stats 真实/启发式 usage、rename_session、项目记忆：默认关无记忆工具、开启后 remember/recall/create_skill、首任务自动注入 top-K、成功自动沉淀/失败跳过、子智能体 memory=False，不含真实 API）。
  - `test_cli.py`：`.env` 加载、缺 key 报错、一次性任务入口、`--context-window` 传递、`/rename` 命令、`memory=True` 透传（main 构造 AgentSession）。
  - `test_tui.py`：format_*/_append_tool_line/_line_style 纯函数、run_tui 冒烟桩（StubConsole/StubLive 离线跑通 on_delta/on_tool 回调）。
  - `test_tui_widgets.py`：ConversationLog/SessionList/SkillList/PromptInput 控件（append/update/clear、行配色 `_line_style`、会话列表刷新、技能列表刷新、`!` 命令模式类与占位符切换）、`_fmt_ctx`/`_footer_stats`（None→""、正常含 cache、无 cache 省略、启发式 `~`）/StatusBar（完整路径 + session 标题渲染）/StatusFooter（render 渲染快捷键 + stats）。
  - `test_tui_worker.py`：AgentWorker 后台线程与 call_from_thread 桥（流式 on_delta/on_tool + on_done，桥接 on_assistant_start/on_tool_start/on_stats 新回调，离线 mock 模型）。
  - `test_tui_app.py`：CodeAgentApp 应用（多轮任务渲染 user/assistant 行与回合顺序、Ctrl+N 新建会话清空对话、`!cmd` 命令执行与超时、`[subagent]` 运行标注、skill 加载标注 `[skill] 加载/✓/✗`、Ctrl+S 技能弹窗选中派发技能任务、Ctrl+P `show=False` 隐藏、命令面板禁用、Ctrl+R 重命名与 Esc 取消、on_stats 更新状态栏，离线 mock 模型）。
  - `test_session.py`：SessionStore 创建/保存/加载/列表/坏文件容错、rename 与标题 pin（pin 不被后续保存覆盖）。
  - `test_workspace.py`：工作区初始化/幂等/损坏容错/touch_session。
  - `test_permissions.py`：规则解析/三态/只读白名单/doom_loop/交互询问。
  - `test_skills.py`：技能扫描/合并/覆盖/frontmatter 解析/加载/SkillRegistry.add（写 SKILL.md、非法 name 抛 ValueError、同名覆盖）。
  - `test_memory.py`：MemoryStore 增查/关键词召回（相关度排序）/无命中空/usage_count 热度 bump 与持久化/中英分词/坏行跳过。
  - `test_web.py`：web_fetch 的公网校验/HTML 提取/fetch 离线 mock + web_search 解析/search 用例。
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
- 演示可用 **TUI 交互展示**（`--interactive`，Textual 全屏：可滚动对话/会话列表面板/快捷键，更直观）；也可先演示纯文本一次性任务。
- 视频脚本要点：
  1. 展示一次性任务输入与流式输出；
  2. 展示 agent 自主调用 read_file / edit_file / run_command；
  3. 可展示 "web_search → web_fetch → 查证" 闭环（搜索发现候选 URL → 抓取详情 → 用来源验证结论）；
  4. 可展示父智能体 dispatch_subagent 派出子智能体完成子任务，子智能体阉割派遣（不能递归派遣）；
  5. 最终用命令验证结果（如跑通测试）。
- 产出物：`README.txt`（≤1000 汉字）+ 演示 mp4（≤200MB）。

## 7. 已知风险与对策

| 风险 | 对策 |
|---|---|
| 模型 tool_calls 格式不标准 | `llm.py` 健壮解析 + `test_llm_parse.py` |
| 长任务上下文溢出 | 预算裁剪策略（见 `context-management.md`） |
| 命令工具卡死 | 超时机制 + 无 TTY |
| 误写真实 API key 进仓库 | 环境变量唯一来源 + 提交前 grep 复核 |
| 模型凭记忆编造外部事实 | web_search 发现候选 URL + web_fetch 查证公网资料 |
| 子智能体越权/无限嵌套 | 阉割双层强制（子会话无派遣 schema + 运行时拒绝，深度恒 1）+ 权限继承（--deny/--ask 对子智能体生效）+ max_iterations=10 |
| 环境不可复现 | uv + `uv.lock` 锁定版本；`uv sync` 一键重建 |
