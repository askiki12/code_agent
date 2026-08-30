# 迭代设计：web_fetch 网络检索工具（SSRF 防护 + 纯文本提取）

> 日期：2026-08-30 ｜ 状态：已批准 ｜ 关联 ADR：ADR-016（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地；最终以 `docs/architecture.md`、`docs/tools.md` 与 `docs/development.md` 为接口/使用权威源。

## 1. 背景与目标

agent 目前 7 个工具全部为本地文件/命令操作，无任何网络能力。面对需要外部事实（库文档、API 约定、最新版本号等）的任务，模型只能凭训练记忆作答，存在幻觉风险。业界（Claude Code WebFetch/WebSearch、OpenCode webfetch/websearch）均提供网络工具。本迭代为 agent 增加第一个网络工具 `web_fetch`：抓取公网网页 → 提取标题+正文+前 N 链接回传模型，作为"避免幻觉"的第一块拼图。

**目标**：agent 可通过 `web_fetch(url)` 读取公网页面可读文本，用于查证外部事实；默认拒绝内网/私网地址与 `file://`，防 SSRF。

**迭代约束（用户要求）**：本迭代只做 `web_fetch`，小步推进；`web_search`（关键词搜索）留待下一轮迭代，与 `web_fetch` 同住 `web.py` 模块。

## 2. 范围

**In scope**
- 新增 `code_agent/web.py`：`is_public_http_url` / `extract_web_content` / `fetch`（唯一联网点，session 可注入）。
- `TOOL_SCHEMAS` 追加 `web_fetch`（7 → 8 个工具）；`_HANDLERS` 注册 handler。
- `SYSTEM_PROMPT` 可用工具清单追加 `web_fetch`。
- 测试、文档同步、ADR-016、真实 API 冒烟。

**Out of scope（本期不做）**
- `web_search`（关键词搜索，下一轮迭代）。
- 沙盒/网络白名单配置化、按域名授权（仅内置公网校验）。
- 内容类型强解析（PDF/图片/JSON 等）——只取 `text/html` / `text/plain`。
- robots.txt / 抓取频率控制 / 缓存。

## 3. 工具接口（`web_fetch`）

```
name: web_fetch
description: Fetch a public web page (http/https) and return its title, readable text and first 10 links. Refuses non-public addresses (internal/private networks, file://).
参数:
  url (string, 必填): 要抓取的公网页面 URL
```

- 返回 `ToolResult`（`ok=true`）output 结构：
  ```
  Title: <页面标题>

  <正文纯文本，多个空白折叠为单个空格，保留段落换行>

  Links:
  - <第 1 个绝对 URL>
  - <第 2 个绝对 URL>
  ...
  ```
- 链接最多 N=10 条；正文/标题为空的页面返回 `(empty page)`，不算失败。
- 输出经全局 `truncate()`（默认 8000 字符）截断并标记 `truncated`。
- 失败时 `ok=false`，output 说明原因（见 §6 错误处理）。

## 4. web.py 模块设计（新增）

```
web.py（纯标准库；网络 I/O 复用已有 requests）
├── @dataclass WebContent
│     title: str
│     text: str
│     links: list[str]
├── is_public_http_url(url: str) -> bool
│     # 代理感知。仅 http/https；字面 IP 直接判公网；localhost/*.localhost/*.local 拒绝；
│     # 配置 HTTP(S) 代理时按主机名放行（代理解析真实主机，兼容 fake-ip 代理），
│     # 无代理时 DNS 解析后所有 IP 均须为公网。
│     # 拒绝：file:// 等非 http 协议；loopback、私网、链路本地、组播、保留段、
│     #   IPv4-mapped IPv6；解析失败返回 False。
├── extract_web_content(html: str, base_url: str) -> WebContent
│     # 纯函数。基于标准库 html.parser.HTMLParser：
│     #   - <title> 取标题；
│     #   - 跳过 script/style/noscript 等非内容元素；
│     #   - 正文为可见文本，空白折叠，块级元素处保留换行；
│     #   - 收集 href 链接（相对地址用 urljoin(base_url, href) 归一化），最多 N=10 条。
├── fetch(url, *, timeout=20.0, max_bytes=2*1024*1024, session=None) -> WebContent
│     # 唯一联网点。session 可注入（离线测试 mock）。
│     #   - 前置 is_public_http_url 校验；
│     #   - UA 头（常见浏览器串）；allow_redirects=True，重定向逐跳重校验公网；
│     #   - 按 max_bytes 限量读取响应体（Content-Length 超限直接拒绝，流式超限截断）；
│     #   - 仅接受 text/html / text/plain（按 Content-Type）；charset 从 header 或 meta 推断，回退 utf-8/errors=replace；
│     #   - 抛 WebFetchError（含原因分类），由上层转 ToolResult。
└── class WebFetchError(Exception)
```

- `is_public_http_url` 与 `extract_web_content` 是纯函数，不联网即可完整单测。
- `fetch` 是本模块唯一真实网络出口；`session=None` 时内部新建 `requests.Session`。

## 5. 集成（tools.py / agent.py）

- `tools.py`：`TOOL_SCHEMAS` 追加 `web_fetch` schema；`_HANDLERS["web_fetch"] = _web_fetch`。
- `_web_fetch(args, workdir) -> ToolResult`：
  - 缺 `url` → `ok=false`（"url is required"）。
  - 调 `web.fetch(url)`；捕获 `WebFetchError`/`requests.RequestException`/`OSError` → `ok=false` 原因回传。
  - 成功 → 组 `Title/正文/Links` 文本，`truncate()` 后返回 `ok=true`。
- `agent.py`：`SYSTEM_PROMPT` 工具清单追加：
  ```
  - web_fetch: fetch a public web page's title/text/links (refuses internal/private addresses)
  ```
  并在 Rules 中追加一条：外部事实（版本号/API 约定/文档）不确定时应优先用 web_fetch 查证，避免凭记忆编造。

## 6. 安全（SSRF 防护）

- **scheme**：仅 `http` / `https`；其余（`file`/`ftp`/`data` 等）拒绝。
- **主机解析**（代理感知）：字面 IP 直接判定；配置 HTTP(S) 代理时按主机名级放行（连接经代理转发，由代理解析真实主机，故跳过 DNS-IP 检查）；无代理时 DNS 解析主机名，**所有**解析结果均须为公网 IPv4/IPv6 地址。拒绝集合：
  - loopback：`127.0.0.0/8`、`::1`；
  - 私网：`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`；
  - 链路本地：`169.254.0.0/16`、`fe80::/10`；
  - 组播/广播/保留：`224.0.0.0/4`、`0.0.0.0/8`、`100.64.0.0/10`(CGNAT)、`fc00::/7`、`::ffff:0:0/96`(IPv4-mapped 按映射 IPv4 判定)；
  - `localhost`/`*.localhost`/`*.local` 及裸 IP 直接判据同上。
  - 决策说明：开发环境使用 Clash 类 fake-ip 代理（DNS 对外部域名返回保留段合成地址 198.18.0.0/16），严格"所有解析 IP 公网"会误拒真实公网站点；代理模式下改为主机名级放行，SSRF 面由代理解析目标 + 重定向逐跳重校验兜底。
- **重定向**：`requests` 逐跳跟随；每一跳的最终目标主机单独重跑公网校验，防 302 跳回内网。
- **资源上限**：`timeout=20s`、`max_bytes=2MB`；`Content-Length` 超限提前拒绝，流式读取超限截断。
- **内容类型**：仅 `text/html` / `text/plain`；PDF/图片等 `ok=false`。
- 凭据：`web.py` 不含任何凭据；URL 仅来自模型参数。

## 7. 错误处理（全部回传模型，不崩溃循环）

| 场景 | 行为 |
|---|---|
| 非公网/非法 URL | `ok=false`："refusing non-public or unsupported URL" |
| 超时 / 连接失败 / DNS 失败 | `ok=false` 带原因摘要 |
| HTTP 4xx/5xx | `ok=false`：`HTTP <status>: <前 200 字响应体>` |
| 非 `text/html`/`text/plain` | `ok=false`：`unsupported content type: <type>` |
| 编码无法解码 | `errors="replace"` 兜底，不失败 |
| 空页面 | `ok=true`，`(empty page)` |
| 响应过大截断 | 截断 + `truncated=true` |

## 8. 测试计划（全部离线，无真实网络/凭据）

**test_web.py（新增）**
1. `is_public_http_url`：
   - 通过：`https://example.com`、`http://example.com/path?q=1`、公网 IP。
   - 拒绝：`file:///etc/passwd`、`ftp://x`、`http://localhost:8000`、`http://127.0.0.1`、`http://10.0.0.1`、`http://192.168.1.1`、`http://[::1]`、裸私网 IP、畸形 URL（缺 scheme/host）。
2. `extract_web_content`：HTML → 标题/正文/链接正确；script/style/noscript 剔除；空白折叠；相对链接归一化（`urljoin`）；链接超 10 条截断到 10。
3. `fetch`（FakeSession mock）：成功抓取；HTTP 404；超时（`Timeout`）；非文本 Content-Type；Content-Length 超限；流式超限截断；重定向逐跳校验（302 到内网被拒）。

**test_tools.py（扩展）**
4. `TOOL_SCHEMAS` 名称集合加入 `web_fetch`（8 个）。
5. `execute("web_fetch", ...)`：monkeypatch `web.fetch`，成功与失败两路断言 `ToolResult`。
6. 缺 `url` 参数 → `ok=false`。

全部离线。冒烟（真实 API）：让 agent 用 `web_fetch` 抓一个文档页并回答一个需外部事实的问题（如确认某库当前版本/某 API 的用途），验证不产生幻觉性编造。

## 9. 文档同步

- `docs/architecture.md`：模块总览加 `web.py`；§3 接口约定（is_public_http_url/extract_web_content/fetch/WebFetchError）；tools 列表更新为 8 个。
- `docs/tools.md`：§1 工具数量 7 → 8；新增 §3.8 web_fetch（参数/返回/SSRF 保护/错误）。
- `docs/design.md`：§6 功能范围勾选 web_fetch；§8 开发路线追加。
- `docs/development.md`：运行/测试说明更新（新增 test_web.py）。
- `code_agent/docs/superpowers/specs/2026-08-30-web-fetch-design.md`（本文档）。
- 工作区根 `.agent/03-decisions.md`：ADR-016（不入库，ADR-007）。

## 10. 开发顺序（小步推进，每步可验证）

1. `web.py` 纯函数（`is_public_http_url` / `extract_web_content`）+ `test_web.py`（TDD）
2. `web.py` 的 `fetch`（FakeSession mock 离线测试）（TDD）
3. `tools.py` 注册 `web_fetch` + `test_tools.py` 扩展（TDD）
4. `agent.py` SYSTEM_PROMPT 更新
5. 文档同步 + ADR-016
6. 真实 API 冒烟（抓一个真实文档页）
7. 全量回归 + 凭据复核 + 提交
