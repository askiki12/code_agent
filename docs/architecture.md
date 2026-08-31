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
| `workspace.py` | 工作区身份与元数据：Workspace（workspace.json 幂等读写/触摸/展示） | 无（纯逻辑，标准库） |
| `permissions.py` | 权限模型：Policy（allow/ask/deny 三态、只读白名单、doom_loop） | 无（纯逻辑，标准库） |
| `skills.py` | 技能库：SkillRegistry（项目+用户级 SKILL.md 扫描/加载） | 无（纯逻辑，标准库） |
| `llm.py` | OpenAI 兼容 API 调用（流式）、响应/工具调用解析、usage/上下文窗口解析、重试 | requests |
| `web.py` | 网络检索：公网 URL 校验（SSRF）、HTML 文本提取、关键词搜索（DDG Lite）、唯一联网点 fetch/search（session 可注入） | requests |
| `tui/` 包 | Textual 终端界面：app.py（CodeAgentApp 全屏应用）、widgets.py（StatusBar/StatusFooter/ConversationLog/SessionList/PromptInput）、worker.py（AgentWorker 后台线程 + call_from_thread 桥）；format_*/_line_style/_fmt_ctx/_footer_stats 纯函数；顶栏完整路径+会话名、底栏 stats、Ctrl+R 重命名 | textual, rich |

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

每回合 chat 成功后 `on_stats(usage)` 回调（真实 usage 或启发式回退）→ TUI StatusFooter 渲染底栏 stats（`_footer_stats`：`213.0k(21%) cache:40%`，启发式 `~`，分母=上下文窗口 W）；切会话/新建会话后 `_refresh_status` 按 `last_usage` 立即刷新底栏。

## 3. 模块接口约定（与实际实现一致）

### cli.py
- `main(argv=None) -> int` — 命令行入口：`_load_dotenv()` → 解析参数 → 构造 Workspace/SessionStore/LLMClient/Policy/SkillRegistry/AgentSession → 分派（`--list-sessions` / `--prompt` / `--interactive`）；`--context-window <n>` 覆盖，否则经 `resolve_context_window` 自动解析，传 `context_window=window` 给 AgentSession。
- `handle_command(command, session, store) -> (keep: bool, out: list[str])` — 交互斜杠命令纯函数（`/new` `/list` `/resume` `/rename <title>` `/exit`），cli 与 tui 共用，便于测试。
- `_use_tui() -> bool` — `stdout.isatty()` 且 `NO_TUI` 未设置；`--interactive` 时 TTY 进 TUI，非 TTY 或 `NO_TUI=1` 回退纯文本 input() 循环。
- `_run(session, task)` — 一次性任务与纯文本交互共用的流式打印。

### tui/ 包（Textual 全屏 TUI，ADR-020）
- `run_tui(session, store, workspace=None, *, model="") -> None` — 构造并启动 `CodeAgentApp`；签名与 rich 版一致，cli 零改动。
- `format_user / format_assistant / format_tool / _append_tool_line / _line_style` — 纯函数：对话行格式化与按前缀着色（user cyan、tool dim、use_skill magenta、dispatch_subagent bold cyan、subagent yellow/green、skill 加载 magenta/✓ green/✗ red、stopped yellow、session magenta），便于离线测试。
- `_fmt_ctx(n) -> str` / `_footer_stats(usage, context_window) -> str` — 纯函数：底栏 stats 紧凑格式化（`213.0k(21%)`，恒一位小数、pct 相对上下文窗口 W、启发式 `~` 前缀、无缓存隐藏 cache 段；替代已删除的 `_fmt_k`/`_usage_segments`），便于离线测试。
- `CodeAgentApp(session, store, workspace=None, *, model="")` — Textual 应用：Header + StatusBar + Horizontal(ConversationLog + SessionList) + PromptInput + StatusFooter(id="footer")；绑定 Ctrl+Q（退出）/ Ctrl+N（新建会话）/ Ctrl+L（切换会话列表面板）/ Ctrl+S（技能选择弹窗）/ Ctrl+R（重命名会话，Esc 取消）/ Ctrl+P（noop 且 `show=False` 彻底隐藏，Footer 不再显示）；`COMMANDS=()` + `ENABLE_COMMAND_PALETTE=False` 移除命令面板；`on_input_submitted` 处理斜杠命令（`/new` `/list` `/resume` `/exit`，复用 `handle_command`）、`!cmd` 终端命令与任务启动；`on_option_list_option_selected` 从会话列表恢复会话；`action_choose_skill` 弹出技能选择（选中回调派发"请加载技能 <name>"任务）；`_workspace_line()` 返回 `Workspace: <完整路径>`；`_refresh_status` 同时刷新顶栏（路径/会话名）与底栏 stats（`last_usage` + `_footer_stats`）。
- `!cmd` 命令模式：`PromptInput.on_input_changed` 检测到输入以 `!` 开头即切换 `command-mode` 类（输入框边框变 warning 色）并改占位符 `❯ shell: 输入命令（回车执行）`，脱离后复原——`!` 开头直接执行 shell 命令（用户主动命令，不走 policy）；后台线程 `subprocess.run(shell=True, timeout=120)`，busy 互斥（运行中拒接新任务/命令），结果回显到对话区并标注 `[exit <code>]` / `[command timed out after 120s]`。
- skill 加载标注：`_on_tool_start` 对 `use_skill` 追加 `[skill] 加载 <name>…` 行（`_line_style` magenta）；`_on_tool` 完成时改标 `[skill] ✓ <name>`（绿）/ `[skill] ✗ <name>`（红）。子智能体运行标注：`_on_tool_start` 对 `dispatch_subagent` 追加 `[subagent] 子智能体运行中…` 行；`_on_tool` 完成时改标 `[subagent] ✓ 完成`（不回显子报告）。
- `SkillScreen`（模态弹窗，ADR-022）：Esc 取消退出；`SkillList` 列出可用技能（name + description），↑↓ 选择、Enter 确认，`on_option_list_option_selected` `event.stop()` 防冒泡后 `dismiss` 选中技能；回调经 `action_choose_skill` → `_on_skill_chosen` 派发技能任务。
- `_on_assistant_start`/`_on_delta`/`_on_done`：每回合 LLM 调用前新建 `assistant:` 行并重钉索引，流式增量就地刷新该行，回合结束用最终文本定型——修复多轮文本合并进第一行的问题。
- `action_toggle_sessions`：切换会话列表面板后重渲染 body 并钳制滚动偏移（`scroll_to(y=min(...))`），修复滚动后布局重叠。
- `AgentWorker(app, session, *, on_delta, on_tool, on_done, on_ask=None, on_ask_timeout=None, on_assistant_start=None, on_tool_start=None, on_stats=None)` — 后台线程执行 `session.run_task`（含 `_ask` 权限询问阻塞等待输入栏应答），所有 UI 更新经 `app.call_from_thread` 桥回主线程，保证 UI 始终响应；`on_assistant_start`/`on_tool_start` 经桥转发（分别对应每回合 / 每工具调用的运行态标注，`on_tool_start(name, arguments)` 携参）；`on_stats(usage)` 经桥转发每回合 usage（驱动底栏 stats）。
- `StatusBar / StatusFooter / ConversationLog / SessionList / SkillList / PromptInput / SkillScreen` — 自定义控件：状态栏（`update_status(state, model, session_title, workspace_line)` 渲染 `Workspace: <完整路径> | model: <model> | session: <会话名> ● <运行态>`，空会话名显示 `new`）、底栏（`StatusFooter` 纯 Widget render 自绘：左侧渲染快捷键（`screen.active_bindings` 取 `show=True`，`^q Quit …`），右侧右对齐紧凑 stats `213.0k(21%) cache:40%`，`update_stats(text)` 存文本 + refresh）、可滚动对话日志（滚轮/PageUp/PageDown，近底自动跟随可回看）、会话列表面板（Ctrl+L 切换）、技能列表面板（Ctrl+S 弹窗）、输入栏（含权限 ask 就地确认 + `!` 命令模式状态反馈 + rename 模式 `set_rename_mode`/`clear_rename_mode`）。

### llm.py
- `Usage` dataclass：`prompt_tokens / completion_tokens=0 / total_tokens=0 / cached_tokens=0 / heuristic=False`；`parse_usage(data)` 解析流式末 chunk usage（无效或缺 prompt_tokens → None）。
- `LLMClient(*, base_url, api_key, model, timeout=300.0, max_retries=3, debug=False, use_usage=True)` — 配置来自环境变量 / `.env` / 命令行。
- `chat(messages, tools=None, on_delta=None) -> LLMResponse` — 流式 SSE，`on_delta` 按增量回调展示；`use_usage=True` 时带 `stream_options.include_usage`，严格网关拒该字段时 `LLMError` 去掉字段重试一次。
- `LLMResponse`：`content: str`、`tool_calls: list[ToolCall]`、`usage: Usage | None`。
- `ToolCall`：`{id: str, name: str, arguments: dict}`。
- `resolve_context_window(model, base_url, api_key, *, get_json=None, default=1_000_000) -> int` — 上下文窗口解析：/models `context_length` → 模型名查表 → 默认 1M，永不抛。
- 错误语义：429/5xx/网络错误指数退避重试（默认 3 次）；非重试性 HTTP 错误与畸形 tool 参数 JSON 抛 `LLMError`。

### tools.py
- `TOOL_SCHEMAS: list[dict]` — 9 个工具（read_file/write_file/edit_file/list_dir/run_command/glob/grep/web_fetch/web_search）的 OpenAI functions JSON Schema。
- 模型可见工具共 10 个：上述 9 个 + `dispatch_subagent`（schema 定义在 agent.py 的 `_DISPATCH_SUBAGENT_SCHEMA`，按 `allow_subagent` 动态注入）。
- `execute(name, args, workdir) -> ToolResult`。
- `ToolResult`：`{ok: bool, output: str, truncated: bool, exit_code?: int}` + `as_message()`。
- 所有输出为纯文本，便于回填给模型；超长自动截断（默认 8000 字符）；受保护路径（`.env*` 除 `.env.example`、`.git`）禁读禁写；写操作限定工作目录内。

### session.py
- `SessionStore(root)` — root 为 `<workdir>/.code_agent/sessions`。
- `list_sessions() -> list[dict]`（按 updated_at 倒序，含 message_count）/ `create(title) -> session_id` / `save(session_id, messages, title=None)`（全量原子写；existing 已 pin 时保持现标题）/ `load(session_id) -> (meta, messages)`（缺失抛 KeyError，坏行跳过）/ `rename(session_id, title)`（置 title + `title_pinned: true` + 更新 updated_at，缺失抛 KeyError）/ `get_title(session_id) -> str`（读首行 meta 的 title，会话缺失或坏 meta 返回空串，不抛）。

### workspace.py
- `Workspace(workdir)` — 读取/初始化 `<workdir>/.code_agent/workspace.json`（id = sha1(realpath)[:12]，name = basename）。
- `path` property — 返回工作区完整路径（realpath），供 TUI 顶栏展示完整路径。
- `touch_session(session_id)`：更新 last_session_id + updated_at（原子写）。
- `display() -> str`："Workspace: <name> (<id>)"；实时统计由 CLI 拼接。

### permissions.py
- `Policy(allow=None, deny=None, ask=None)` — 规则 `tool:pattern`（fnmatch）。
- `check(tool, arguments, interact=False, ask=None) -> PermissionResult`：deny→ask→allow→默认 allow；run_command 应用只读白名单；doom_loop（连续相同调用 ≥3）→ deny。
- 交互询问：`[permission] ... [y/N]`，y→allow 其余→deny；非交互 ask→deny。`ask` 为可选回调（缺省 `input()`，纯文本不变）；TUI 传入渲染回调在输入栏确认。

### skills.py
- `SkillRegistry(project_dir, user_dir=None)` — 扫描 `<workdir>/.code_agent/skills/` 与 `~/.code_agent/skills/`（同名项目优先）。
- `scan() -> list[Skill]`（按 name 排序）/ `load(name) -> str | None`（SKILL.md 全文）。
- SKILL.md frontmatter：`name` / `description`；缺失或非法跳过并警告。

### web.py
- `is_public_http_url(url) -> bool`：仅公网 http/https；代理感知：无代理时 DNS 后所有 IP 公网，有代理时主机名级校验（代理解析真实主机）；拒 file://、私网/回环/链路本地/保留段。
- `extract_web_content(html, base_url) -> WebContent`（title/text/links，前 10 链接，纯函数）。
- `fetch(url, *, timeout=20.0, max_bytes=2MB, session=None) -> WebContent`：唯一联网点；逐跳重定向校验；失败抛 `WebFetchError`。
- `search(query, *, max_results=8, timeout=20.0, max_bytes=2MB, session=None) -> list[SearchResult]`（DDG Lite，重试 3 次，clamp 1..10）。
- `parse_search_results(html) -> list[SearchResult]`（纯函数，uddg 解码，仅 http(s)）。
- `SearchResult`：`title/url/snippet`。
- `WebFetchError(Exception)`。

### context.py
- `Conversation` 类：内部维护消息列表（system/user/assistant/tool）。
- `add_system` / `add_user` / `add_assistant(content, tool_calls=None)` / `add_tool(tool_call_id, name, output)`。
- `messages`（返回拷贝）、`is_valid()`（结构一致性校验）。
- `build_messages(max_tokens) -> list` — 估算 token，超限按组裁剪（assistant+其后 tool 成组，不产生孤儿 tool）。
- `estimate_tokens(text) -> int` — 启发式估算（CJK≈1 token，其它≈每 4 字符 1 token）。

### agent.py
- `AgentSession(*, workdir, llm, max_iterations=20, max_context_tokens=90000, debug=False, store=None, session_id=None, resume=False, workspace=None, policy=None, interact=False, ask=None, skills=None, allow_subagent=True, context_window=None)` — `llm` 依赖注入，便于测试；`store`/`workspace`/`policy`/`interact`/`ask`/`skills` 均可选（无则不启用对应能力）。
- `context_window` 属性（默认 1_000_000）；`max_context_tokens` = `min(CLI 值, int(0.7 × context_window))`（仅 context_window 提供时应用）；`last_usage: Usage | None` — 最近回合真实/启发式 usage。
- `current_title() -> str` — 当前会话标题（经 `SessionStore.get_title`；无 store 或尚未创建会话返回空串，TUI 顶栏据此显示 `new`）。
- `load_session(session_id)` — 恢复会话时用 `estimate_tokens` 对全部历史消息设**启发式** `last_usage`（切会话后 stats 立即刷新为该会话估算值）；`new_session()` — 新建会话时**清空** `last_usage`（防止残留上一会话 stats）。
- `rename_session(title) -> str` — 会话重命名：无 store 抛 `ValueError`、空 title 抛 `ValueError`；无 session_id 先 `store.create(title)`，再 `store.rename`（pin）。
- `ask`（可选回调 `(prompt) -> str`）透传给 `Policy.check` 作权限询问实现，并透传给子会话（subagent）；缺省 `input()`。
- `use_skill` 工具在 skills 存在时注册；system prompt 注入技能列表（Available skills），加载的 SKILL.md 全文回传模型。
- `dispatch_subagent` 工具（schema `_DISPATCH_SUBAGENT_SCHEMA`，仅 `task` 参数）在 `allow_subagent=True` 时注入模型工具列表。
- `_dispatch_subagent(arguments) -> ToolResult` — 构造子会话（继承 workdir/llm/policy/interact/skills，`max_iterations=SUBAGENT_MAX_ITERATIONS=10`，`allow_subagent=False`，不带 store/workspace 故不持久化）跑同步嵌套循环，只回传最终报告（空报告回传 status，按 8000 字符截断）。
- 子智能体阉割派遣为双层强制、深度恒 1：① 子会话工具列表不含 `dispatch_subagent` schema（模型不可见）；② `_run_tool` 运行时对 `allow_subagent=False` 会话的 `dispatch_subagent` 调用直接返回 `ToolResult(ok=False)` 拒绝。
- 权限继承：`policy`/`interact` 透传给子会话，`--deny`/`--ask` 规则对子智能体同样生效，防止绕过权限；子会话 system prompt 追加 `SUBAGENT_PROMPT_EXTRA`（subagent 指示）。
- `run_task(task, on_delta=None, on_tool=None, on_assistant_start=None, on_tool_start=None, on_stats=None) -> RunResult` — 主循环；`on_delta` 流式增量展示；`on_assistant_start` 每回合 LLM 调用前回调（TUI 用于新建 assistant 行）；`on_tool_start(name, arguments)` 每工具调用前回调（携参，TUI 用于子智能体运行标注与 skill 加载标注）；`on_tool` 回调 `(name, ToolResult)` 逐工具调用；`on_stats(usage)` 每回合 chat 成功后回调（usage 缺失时启发式 `Usage(..., heuristic=True)`，并写 `last_usage`）；`RunResult` 含 `final_text/iterations/finished/reason`。
- 终止条件（三条）：无 tool_calls（`complete`）／达到 `max_iterations`／连续失败（工具或 LLM 错误）达 3 次。
- 错误恢复：工具异常包装为 `ToolResult(ok=False)` 回传模型；`LLMError` 注入修复提示并计数，达阈值优雅终止。

## 4. 设计原则

- 每层只依赖相邻层的接口，可独立单元测试。
- 工具执行器是纯本地逻辑，绝不调用任何服务端托管工具。
- 输出均为流式文本，CLI 与 agent 解耦。
- 变更模块内部实现时，不破坏对外接口约定。
