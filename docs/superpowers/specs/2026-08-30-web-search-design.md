# 迭代设计：web_search 关键词搜索工具（DuckDuckGo Lite 免 key）

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-017（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/tools.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

web_fetch（ADR-016）解决了"给定 URL 抓取内容"，但 agent 仍需知道**该抓哪个 URL**——面对不熟悉的主题，只能凭记忆猜 URL，仍可能编造。业界（Claude Code WebSearch、OpenCode websearch）提供关键词搜索作为发现入口。本迭代为 agent 增加 `web_search`：关键词搜索（免 key）→ 返回标题+真实 URL+摘要，模型随后可用 `web_fetch` 抓详情，形成"搜索→抓取→查证"闭环。

**目标**：agent 可通过 `web_search(query)` 获取网页搜索结果列表（标题/URL/摘要），作为 web_fetch 的发现前置。

**迭代约束（用户决定）**：后端选 **DuckDuckGo Lite 免 key**（已验证连通：`lite.duckduckgo.com/lite/?q=...` 经代理可达，返回 `<a class='result-link'>` 标题 + `<td class='result-snippet'>` 摘要，真实 URL 藏在 `uddg` 查询参数）；实现结构为 **web.py 加 `search` + 纯解析**。

## 2. 范围

**In scope**
- `code_agent/web.py` 新增 `SearchResult` / `parse_search_results(html)`（纯函数）/ `search(query, *, max_results=8, ...)`（唯一新增联网点，session 可注入，失败重试）。
- `TOOL_SCHEMAS` 追加 `web_search`（8 → 9 个工具）；`_HANDLERS` 注册 handler。
- `agent.py` SYSTEM_PROMPT 工具清单与查证规则更新。
- 测试、文档同步、ADR-017、真实 API 冒烟（搜索→抓取闭环）。

**Out of scope（本期不做）**
- 多后端抽象/可配置搜索引擎（仅 DDG Lite，YAGNI）。
- 分页、语言/地区参数、时间过滤。
- 缓存、robots.txt、搜索配额统计。
- Bing/Google HTML 抓取（重定向 URL 难还原、反爬重）。

## 3. 工具接口（`web_search`）

```
name: web_search
description: Search the web (DuckDuckGo Lite, keyless). Returns numbered results with title, real URL and snippet. Use web_fetch on a result URL for full content.
参数:
  query (string, 必填): 搜索关键词
  max_results (integer, 可选, 默认 8): 返回条数上限（clamp 1..10）
```

- 返回 `ToolResult`（`ok=true`）output 结构（空行分隔）：
  ```
  1. <标题>
  <真实 URL>
  <摘要>

  2. <标题>
  ...
  ```
- 无结果：`(no results)`（`ok=true`，不算失败）。
- 每条 snippet 上限 200 字符（超长截断加 `...`）；缺 snippet 时置空串仍返回该条。
- 整体输出经全局 `truncate()`（8000 字符）截断并标记 `truncated`。
- 失败时 `ok=false`，output 说明原因（见 §7）。

## 4. web.py 扩展（新增）

```
web.py（追加）
├── @dataclass SearchResult
│     title: str
│     url: str
│     snippet: str
├── parse_search_results(html: str) -> list[SearchResult]
│     # 纯函数，不联网。基于 html.parser.HTMLParser 状态机：
│     #   - <a class='result-link' href='//duckduckgo.com/l/?uddg=<urlencoded>&rut=...'> 采集标题与 href；
│     #   - href 解析 uddg 查询参数并 unquote 还原真实 URL；仅保留 http(s)；
│     #   - 后续 <td class='result-snippet'> 的文本挂到最近一条结果（DDG Lite 结构：结果行后紧跟摘要行）；
│     #   - 标题/摘要折叠空白去标签；snippet 超 200 字符截断。
├── search(query, *, max_results=8, timeout=20.0, max_bytes=2MB, session=None) -> list[SearchResult]
│     # 唯一新增联网点。
│     #   - 构建 https://lite.duckduckgo.com/lite/?q=<urlencode(query)>；
│     #   - 复用 _request_with_validation（代理感知 + 逐跳公网校验 + 重定向上限）与 _read_limited（限量）；
│     #   - 仅接受 text/html；
│     #   - DDG Lite 偶发 TLS 波动 → 失败重试最多 3 次（间隔 1s）；
│     #   - max_results clamp 到 1..10，结果截取前 N 条；
│     #   - session 可注入（离线测试）；最终失败抛 WebFetchError。
└── 复用：WebFetchError / is_public_http_url / _request_with_validation / _read_limited / _guess_charset
```

- `parse_search_results` 为纯函数，离线可完整单测。
- `search` 是本模块第二个联网点（复用既有安全辅助），仅查询固定主机 `lite.duckduckgo.com`。

## 5. 集成（tools.py / agent.py）

- `tools.py`：`TOOL_SCHEMAS` 追加 `web_search` schema；`_HANDLERS["web_search"] = _web_search`。
- `_web_search(args, workdir) -> ToolResult`：
  - 缺/空白 `query` → `ok=false`（"query is required"）。
  - 调 `web.search(query, max_results=args.get("max_results", 8))`；`max_results` 非 int/越界 → 用默认 8。
  - `WebFetchError` → `ok=false`（"web_search failed: ..."）。
  - 成功 → 组编号列表文本（标题/URL/摘要），`truncate()` 截断。
- `agent.py` SYSTEM_PROMPT：
  - 工具清单追加：`- web_search: search the web (DuckDuckGo Lite) returning titles/URLs/snippets`。
  - Rules 追加/修订：`When external facts are uncertain, search the web with web_search to discover candidate URLs, then use web_fetch to read the full page — never guess from memory.`

## 6. 安全

- `search` 仅查询**固定主机** `lite.duckduckgo.com`，沿用既有代理感知 + 逐跳公网校验 + 限量读取链路；不新增 SSRF 面。
- 返回的 URL 只是数据；模型要抓取必须走 `web_fetch`（自带 SSRF 防护）。
- 还原 URL 时仅保留 `http(s)` 协议，过滤 `javascript:`/`file:` 等。
- 无凭据处理；`web.py` 不含凭据。

## 7. 错误处理（全部回传模型，不崩溃循环）

| 场景 | 行为 |
|---|---|
| 缺 `query` / 空白 | `ok=false`："query is required" |
| 网络失败（TLS/超时/4xx/5xx） | 重试 3 次后 `ok=false` 带原因 |
| 结果页无可解析条目（空页/反爬页/非 html） | `ok=true`，`(no results)` |
| 部分条目缺 snippet | snippet 置空串，仍返回该条 |
| snippet 超 200 字符 | 截断加 `...` |
| 整体输出超 8000 字符 | `truncate()` 截断 + `truncated=true` |

## 8. 测试计划（全部离线，无真实网络/凭据）

**test_web.py（扩展）**
1. `parse_search_results`：DDG Lite 样例 HTML → 标题/URL/snippet 正确；`uddg` 解码还原真实 URL。
2. `parse_search_results`：`javascript:`/`file:` 等非 http(s) 结果过滤。
3. `parse_search_results`：snippet 挂接最近一条结果（两条结果 + 两条摘要顺序正确）。
4. `parse_search_results`：snippet 超长截断 200 字符；缺 snippet 置空串。
5. `parse_search_results`：无可解析内容 → `[]`。
6. `search`（FakeSession）：成功返回列表（条数按 max_results）。
7. `search`：网络失败重试（前 2 次抛异常、第 3 次成功）→ 返回结果。
8. `search`：持续失败 → `WebFetchError`。
9. `search`：`max_results` 越界 clamp（0→1、99→10）。

**test_tools.py（扩展）**
10. `TOOL_SCHEMAS` 名称集合 8 → 9（加 `web_search`）。
11. `execute("web_search", ...)`：monkeypatch `web.search` 成功 → 输出含编号/标题/URL/摘要。
12. `execute("web_search", ...)`：空结果 → `(no results)`。
13. `execute("web_search", ...)`：`WebFetchError` → `ok=false`。
14. 缺 `query` → `ok=false`。

全部离线。冒烟（真实 API）：agent 用 `web_search` 搜一个主题 → 从结果中选一条 → `web_fetch` 抓详情 → 报告带来源的事实，验证"搜索→抓取→查证"闭环。

## 9. 文档同步

- `docs/architecture.md`：模块总览/§3 加 `search` / `parse_search_results` / `SearchResult`；tools 列表 8 → 9。
- `docs/tools.md`：§1 工具数量 8 → 9；新增 §3.9 web_search（参数/返回/限制/与 web_fetch 配合）。
- `docs/design.md`：§6 功能范围勾选；§8 开发路线追加（14）。
- `docs/development.md`：测试目录说明追加 web_search 用例；演示/风险表补充。
- `code_agent/docs/superpowers/specs/2026-08-30-web-search-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-017（不入库，ADR-007）。

## 10. 开发顺序（小步推进，每步可验证）

1. `web.py` `SearchResult` + `parse_search_results` + `test_web.py`（TDD）
2. `web.py` `search`（FakeSession 离线测试，含重试/clamp）（TDD）
3. `tools.py` 注册 `web_search` + `test_tools.py` 扩展（TDD）
4. `agent.py` SYSTEM_PROMPT 更新
5. 文档同步 + ADR-017
6. 真实 API 冒烟（搜索→抓取→查证闭环）
7. 全量回归 + 凭据复核 + 提交
