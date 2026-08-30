# web_search 关键词搜索工具实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 agent 新增 `web_search` 工具：关键词搜索（DuckDuckGo Lite 免 key）→ 返回编号的标题+真实 URL+摘要，模型随后用 web_fetch 抓详情，形成"搜索→抓取→查证"闭环。

**Architecture:** 在 `web.py` 新增纯函数 `parse_search_results(html)`（HTMLParser 状态机解析 DDG Lite 结果页）+ `search()`（复用 `_request_with_validation`/`_read_limited` 的代理/SSRF/限量链路，唯一新增联网点，失败重试 3 次，session 可注入）；`tools.py` 注册 `web_search`（9 个工具）；`agent.py` SYSTEM_PROMPT 同步。

**Tech Stack:** Python 3.11+，`requests`（唯一第三方依赖），`pytest`（开发）。所有测试离线（FakeSession + 固定 HTML 样例，无真实网络/凭据）。

## Global Constraints

- 禁止任何 agent 框架/SDK；禁止服务端托管工具；重要逻辑自实现。
- 后端仅 `lite.duckduckgo.com/lite/?q=`（固定主机，经既有代理感知 + 逐跳公网校验链路）；不新增 SSRF 面。
- 返回每条：标题 + 真实 URL（仅 http(s)，`uddg` 解码还原）+ 摘要（≤200 字符截断）；默认 `max_results=8`，clamp 1..10。
- 整体输出经 `truncate()`（8000 字符）截断；空结果返回 `(no results)`（ok=true）。
- 失败（TLS/超时/4xx/5xx）内部重试最多 3 次（间隔 1s），仍失败抛 `WebFetchError` → 工具 `ok=false`。
- `search` 失败一律抛 `WebFetchError`（不泄漏 requests/OSError）；session 可注入（离线测试）。
- 凭据唯一来源环境变量/`.env`；`web.py` 不含凭据。
- 工具 schema 以 `docs/tools.md` 为权威源（本迭代同步更新）。
- 测试全部离线，禁止真实 API key；每个任务结束必须测试通过并提交（保留完整历史，不 rebase/改写）。

---

### Task 1: web.py —— `SearchResult` + `parse_search_results`（纯函数）

**Files:**
- Modify: `code_agent/code_agent/web.py`（追加 `SearchResult`、`_extract_uddg_url`、`_SearchParser`、`parse_search_results`；`import time` 不加，Task 2 才用；`from urllib.parse import parse_qs` 追加）
- Modify: `code_agent/tests/test_web.py`（追加解析用例）

**Interfaces:**
- Consumes: 既有 `WebFetchError`、`urlparse`。
- Produces:
  - `@dataclass SearchResult`：`title: str` / `url: str` / `snippet: str`
  - `parse_search_results(html: str) -> list[SearchResult]`——纯函数；解析 DDG Lite 结果页：`<a class='result-link' href='//duckduckgo.com/l/?uddg=<urlencoded>&rut=...'>` 取标题+URL（`uddg` 参数 parse_qs 解码还原，仅保留 http(s)），随后 `<td class='result-snippet'>` 文本挂到最近一条结果；snippet 折叠空白、超 200 字符截断加 `...`；缺 snippet 置空串。
  - `SEARCH_MAX_RESULTS = 8` / `SEARCH_BASE = "https://lite.duckduckgo.com/lite/"`（常量，Task 2 使用）。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_web.py`：

```python
from code_agent.web import SearchResult, parse_search_results

_SAMPLE = """<html><head><title>DuckDuckGo</title></head><body>
<table border="0">
<tr><td valign="top">1.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python-requests.org%2Fen%2Fmaster%2Findex.html&amp;rut=abc" class='result-link'>Requests: HTTP for Humans — Requests 2.34.2 documentation</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'><b>Requests</b>: HTTP for Humans. <b>Requests</b> is an elegant and simple HTTP library.</td></tr>
<tr><td valign="top">2.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=javascript%3Aalert(1)&amp;rut=def" class='result-link'>Bad link</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>should be filtered out</td></tr>
<tr><td valign="top">3.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2Fproject%2Frequests%2F&amp;rut=ghi" class='result-link'>requests · PyPI</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>The Python Package Index page for requests.</td></tr>
</table>
</body></html>"""


def test_parse_search_results_basic():
    results = parse_search_results(_SAMPLE)
    assert [r.title for r in results] == [
        "Requests: HTTP for Humans — Requests 2.34.2 documentation",
        "requests · PyPI",
    ]
    assert results[0].url == "https://docs.python-requests.org/en/master/index.html"
    assert results[1].url == "https://pypi.org/project/requests/"
    assert results[0].snippet == (
        "Requests: HTTP for Humans. Requests is an elegant and simple HTTP library."
    )


def test_parse_search_results_snippet_attached_to_right_result():
    results = parse_search_results(_SAMPLE)
    assert results[0].snippet.startswith("Requests:")
    assert results[1].snippet == "The Python Package Index page for requests."


def test_parse_search_results_filters_non_http():
    urls = [r.url for r in parse_search_results(_SAMPLE)]
    assert "javascript:alert(1)" not in urls
    assert all(u.startswith(("http://", "https://")) for u in urls)


def test_parse_search_results_snippet_truncated():
    html = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F" '
        "class='result-link'>T</a>"
        f"<td class='result-snippet'>{'y' * 250}</td>"
    )
    results = parse_search_results(html)
    assert len(results) == 1
    assert len(results[0].snippet) == 203
    assert results[0].snippet.endswith("...")


def test_parse_search_results_missing_snippet_empty_string():
    html = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F" '
        "class='result-link'>T</a>"
    )
    results = parse_search_results(html)
    assert len(results) == 1
    assert results[0].snippet == ""


def test_parse_search_results_empty_html():
    assert parse_search_results("") == []
    assert parse_search_results("<html><body>no results here</body></html>") == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL（`ImportError: cannot import name 'parse_search_results'`）

- [ ] **Step 3: 最小实现**

在 `code_agent/code_agent/web.py` 末尾追加：

```python
from urllib.parse import parse_qs

SEARCH_MAX_RESULTS = 8
SEARCH_BASE = "https://lite.duckduckgo.com/lite/"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _extract_uddg_url(href: str) -> str | None:
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    values = parse_qs(parsed.query).get("uddg")
    if not values:
        return None
    url = values[0]
    if url.startswith(("http://", "https://")):
        return url
    return None


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_link = False
        self._link_title: list[str] = []
        self._link_url: str | None = None
        self._in_snippet = False
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "a" and attr.get("class") == "result-link":
            self._in_link = True
            self._link_title = []
            self._link_url = _extract_uddg_url(attr.get("href") or "")
        elif tag == "td" and attr.get("class") == "result-snippet":
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = " ".join("".join(self._link_title).split())
            if self._link_url:
                self.results.append(SearchResult(title=title, url=self._link_url, snippet=""))
            self._in_link = False
        elif tag == "td" and self._in_snippet:
            snippet = " ".join("".join(self._snippet_parts).split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            if self.results and not self.results[-1].snippet:
                self.results[-1].snippet = snippet
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_title.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


def parse_search_results(html: str) -> list[SearchResult]:
    parser = _SearchParser()
    parser.feed(html)
    return parser.results
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS（本 Task 全部用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/web.py tests/test_web.py
git commit -m "feat: web.py 搜索结果解析 parse_search_results（标题/URL/摘要，Task 1/6）"
```

---

### Task 2: web.py —— `search`（DDG Lite 抓取 + 重试 + clamp）

**Files:**
- Modify: `code_agent/code_agent/web.py`（追加 `search`；`import time`、`from urllib.parse import urlencode`）
- Modify: `code_agent/tests/test_web.py`（追加 search 用例）

**Interfaces:**
- Consumes: Task 1 的 `SearchResult` / `parse_search_results` / `SEARCH_MAX_RESULTS` / `SEARCH_BASE`；既有 `_request_with_validation` / `_read_limited` / `_guess_charset` / `WebFetchError` / `DEFAULT_TIMEOUT` / `MAX_BYTES` / `USER_AGENT`。
- Produces:
  - `search(query: str, *, max_results: int = SEARCH_MAX_RESULTS, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_BYTES, session=None) -> list[SearchResult]`——GET `SEARCH_BASE?q=<urlencode>`；`max_results` clamp 1..10；结果截取前 N 条；失败重试最多 3 次（间隔 1s）；最终失败抛 `WebFetchError`；session 可注入（实现 `.get(...)` 返回带 `.status_code`/`.headers`/`.url`/`.iter_content(chunk_size)`/`.close()` 的响应）。

- [ ] **Step 1: 写失败测试**

追加到 `code_agent/tests/test_web.py`：

```python
from code_agent.web import WebFetchError, search


def _html_with(n):
    rows = []
    for i in range(1, n + 1):
        rows.append(
            f'<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{i}" '
            f"class='result-link'>R{i}</a>"
        )
    return "".join(rows)


def test_search_success(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(3).encode())
    sess = FakeSession([resp])
    results = search("python requests", session=sess)
    assert len(results) == 3
    assert results[0].url == "https://example.com/1"
    assert "lite.duckduckgo.com/lite/" in sess.requests[0]


def test_search_clamps_max_results(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(5).encode())
    r0 = search("q", max_results=0, session=FakeSession([resp]))
    assert len(r0) == 1
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(5).encode())
    r99 = search("q", max_results=99, session=FakeSession([resp]))
    assert len(r99) == 5


def test_search_retries_then_succeeds(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    sess = FakeSession([
        requests.Timeout("boom1"),
        requests.Timeout("boom2"),
        FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(2).encode()),
    ])
    results = search("q", session=sess)
    assert len(results) == 2


def test_search_persistent_failure(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    sess = FakeSession([requests.Timeout("boom")] * 3)
    with pytest.raises(WebFetchError, match="after 3 attempts"):
        search("q", session=sess)


def test_search_empty_query():
    with pytest.raises(WebFetchError, match="query is required"):
        search("   ")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_web.py::test_search_success -v`
Expected: FAIL（`ImportError: cannot import name 'search'`）

- [ ] **Step 3: 最小实现**

在 `code_agent/code_agent/web.py` 末尾追加：

```python
import time
from urllib.parse import urlencode


def search(
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    session=None,
) -> list[SearchResult]:
    """Search DuckDuckGo Lite and return parsed results. Raises WebFetchError."""
    if not query.strip():
        raise WebFetchError("query is required")
    max_results = max(1, min(10, max_results))
    close_session = session is None
    if session is None:
        session = requests.Session()
    url = f"{SEARCH_BASE}?{urlencode({'q': query})}"
    last_error: Exception | None = None
    try:
        for attempt in range(3):
            try:
                resp = _request_with_validation(session, url, timeout)
                try:
                    if resp.status_code != 200:
                        raise WebFetchError(f"HTTP {resp.status_code}")
                    content_type = resp.headers.get("Content-Type", "").lower().split(";")[0].strip()
                    if content_type not in ("text/html",) and content_type:
                        raise WebFetchError(f"unsupported content type: {content_type}")
                    body = _read_limited(resp, max_bytes)
                    charset = _guess_charset(resp.headers.get("Content-Type", ""), body)
                    html = body.decode(charset, errors="replace")
                    return parse_search_results(html)[:max_results]
                finally:
                    resp.close()
            except WebFetchError as e:
                last_error = e
            except requests.RequestException as e:
                last_error = WebFetchError(f"request failed: {e}")
            except OSError as e:
                last_error = WebFetchError(f"network error: {e}")
            if attempt < 2:
                time.sleep(1)
        raise WebFetchError(f"search failed after 3 attempts: {last_error}")
    finally:
        if close_session:
            session.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS（本 Task 全部用例）

- [ ] **Step 5: 提交**

```bash
git add code_agent/web.py tests/test_web.py
git commit -m "feat: web.py 搜索 search（DDG Lite，重试/clamp，session 可注入，Task 2/6）"
```

---

### Task 3: tools.py —— 注册 `web_search` 工具

**Files:**
- Modify: `code_agent/code_agent/tools.py`（schema 追加、`_web_search` handler、`_HANDLERS` 注册）
- Modify: `code_agent/tests/test_tools.py`（schema 集合 8→9；追加用例）

**Interfaces:**
- Consumes: Task 2 的 `web.search` / `web.SearchResult` / `web.WebFetchError`；既有 `ToolResult` / `truncate` / `_schema`。
- Produces: `TOOL_SCHEMAS` 增至 9 个（含 `web_search`）；`_HANDLERS["web_search"]`。

- [ ] **Step 1: 写失败测试**

修改 `code_agent/tests/test_tools.py` 的 `test_tool_schemas_have_expected_names` 断言集合为 9 个（加 `"web_search"`），并追加：

```python
from code_agent.web import SearchResult, WebFetchError


def test_web_search_success(workdir, monkeypatch):
    def fake_search(query, max_results=8, *args, **kwargs):
        return [
            SearchResult(title="Requests docs", url="https://docs.python-requests.org/", snippet="HTTP for Humans"),
            SearchResult(title="PyPI", url="https://pypi.org/project/requests/", snippet="Package page"),
        ]

    monkeypatch.setattr("code_agent.tools.web.search", fake_search)
    r = execute("web_search", {"query": "python requests"}, workdir)
    assert r.ok
    assert "1. Requests docs" in r.output
    assert "https://pypi.org/project/requests/" in r.output
    assert "HTTP for Humans" in r.output


def test_web_search_empty_results(workdir, monkeypatch):
    monkeypatch.setattr("code_agent.tools.web.search", lambda *a, **k: [])
    r = execute("web_search", {"query": "nothing"}, workdir)
    assert r.ok and "(no results)" in r.output


def test_web_search_failure(workdir, monkeypatch):
    def boom(*a, **k):
        raise WebFetchError("search failed after 3 attempts: request failed")

    monkeypatch.setattr("code_agent.tools.web.search", boom)
    r = execute("web_search", {"query": "x"}, workdir)
    assert not r.ok and "web_search failed" in r.output


def test_web_search_missing_query(workdir):
    r = execute("web_search", {}, workdir)
    assert not r.ok and "query is required" in r.output
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL（schema 集合缺 `web_search`；`unknown tool`）

- [ ] **Step 3: 最小实现**

在 `code_agent/code_agent/tools.py` 的 `TOOL_SCHEMAS` 末尾（`web_fetch` schema 之后）追加：

```python
    _schema(
        "web_search",
        "Search the web (DuckDuckGo Lite, keyless). Returns numbered results with title, real URL and snippet. Use web_fetch on a result URL for full content.",
        {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results (default 8)"},
        },
        ["query"],
    ),
```

在 `_web_fetch` 之后、`_HANDLERS` 之前追加 handler：

```python
def _web_search(args: dict, workdir: str) -> ToolResult:
    query = args.get("query", "")
    if not query.strip():
        return ToolResult(ok=False, output="query is required")
    max_results = args.get("max_results", 8)
    if not isinstance(max_results, int):
        max_results = 8
    try:
        results = web.search(query, max_results=max_results)
    except web.WebFetchError as e:
        return ToolResult(ok=False, output=f"web_search failed: {e}")
    if not results:
        return ToolResult(ok=True, output="(no results)")
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(r.url)
        lines.append(r.snippet)
    out, truncated = truncate("\n\n".join(lines))
    return ToolResult(ok=True, output=out, truncated=truncated)
```

`_HANDLERS` 更新为（追加 `web_search`）：

```python
_HANDLERS = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "run_command": _run_command,
    "glob": _glob,
    "grep": _grep,
    "web_fetch": _web_fetch,
    "web_search": _web_search,
}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS（含 9 个工具 schema 断言）

- [ ] **Step 5: 提交**

```bash
git add code_agent/tools.py tests/test_tools.py
git commit -m "feat: tools.py 注册 web_search 工具（9 个工具，Task 3/6）"
```

---

### Task 4: agent.py —— SYSTEM_PROMPT 更新

**Files:**
- Modify: `code_agent/code_agent/agent.py`（SYSTEM_PROMPT）

**Interfaces:**
- Consumes: 无。
- Produces: 无（纯文本变更；`TOOL_SCHEMAS` 已含 web_search）。

- [ ] **Step 1: 修改 SYSTEM_PROMPT**

在 `code_agent/code_agent/agent.py` 的 SYSTEM_PROMPT 工具清单 `- web_fetch: ...` 之后追加一行；并把 rule 6 改为搜索查证规则：

```python
SYSTEM_PROMPT = """You are a coding agent. You work inside a local workspace and complete software tasks autonomously.

Available tools:
- read_file: read text files
- write_file: create or overwrite files
- edit_file: replace an exact substring in a file (must match uniquely)
- list_dir: list directory contents
- run_command: run a shell command (has a timeout)
- glob: find files by glob pattern (e.g. **/*.py)
- grep: search file contents with a regex
- web_fetch: fetch a public web page's title/text/links (refuses internal/private addresses)
- web_search: search the web (DuckDuckGo Lite) returning titles/URLs/snippets

Rules:
1. Plan each step. Prefer small, verifiable changes.
2. Use run_command to verify your work (e.g. run tests).
3. When a tool fails, read the error, adjust, and retry. Do not repeat the exact same failing call.
4. Do NOT read or write protected paths such as .env, .env.* or .git.
5. When the task is complete, reply with a short final summary and stop making tool calls.
6. When external facts are uncertain, use web_search to discover candidate URLs, then use web_fetch to read the full page — never guess from memory.
"""
```

- [ ] **Step 2: 运行确认现有测试通过**

Run: `uv run pytest tests/ -q`
Expected: PASS（基线 191 + 本迭代新增用例全绿）

- [ ] **Step 3: 提交**

```bash
git add code_agent/agent.py
git commit -m "feat: agent.py 注入 web_search 工具说明与搜索查证规则（Task 4/6）"
```

---

### Task 5: 文档同步 + ADR-017

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/tools.md`
- Modify: `code_agent/docs/design.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/README.md`
- Modify: `../.agent/03-decisions.md`（工作区根，不入库）

**Interfaces:**
- Consumes: Task 1-4 的实现。
- Produces: 文档权威源与实现一致；ADR-017。

- [ ] **Step 1: architecture.md**

- 模块总览 web.py 行更新为：`| web.py | 网络检索：公网 URL 校验（SSRF）、HTML 文本提取、关键词搜索（DDG Lite）、唯一联网点 fetch/search（session 可注入） | requests |`。
- §3 web.py 追加：`search(query, *, max_results=8, timeout=20.0, max_bytes=2MB, session=None) -> list[SearchResult]`（DDG Lite，重试 3 次，clamp 1..10）、`parse_search_results(html) -> list[SearchResult]`（纯函数，uddg 解码，仅 http(s)）、`SearchResult(title/url/snippet)`。
- tools.py `TOOL_SCHEMAS` 数量 8 → 9。

- [ ] **Step 2: tools.md**

- §1 总则：工具数量 8 → 9，名称列表加 `web_search`。
- 新增 §3.9 web_search：参数（`query` 必填、`max_results` 默认 8 clamp 1..10）；返回编号列表（标题/真实 URL/摘要 ≤200 字符）；`(no results)`；8000 字符截断；后端 `lite.duckduckgo.com`（固定主机，经既有安全链路）；与 web_fetch 配合（搜索→抓取→查证）。

- [ ] **Step 3: design.md**

- §6 功能范围勾选：`- [x] web_search 关键词搜索（DDG Lite 免 key，ADR-017）`。
- §8 开发路线追加：`14. [x] 迭代增强：web_search 关键词搜索（ADR-017，设计见 docs/superpowers/specs/2026-08-30-web-search-design.md）`。
- 更新测试计数到实际值（以 Task 6 验证为准，若已执行则填实际，否则填"见 Task 6"前先记录预估，最终以实际为准）。

- [ ] **Step 4: development.md**

- §3 测试目录说明：test_web.py 追加 web_search 解析/search 用例。
- §6/§7：演示任务可展示"web_search → web_fetch → 查证"闭环；风险表"模型凭记忆编造外部事实"对策补 web_search。

- [ ] **Step 5: .agent/03-decisions.md（工作区根）**

追加 ADR-017（格式沿用前序）：

```markdown
## ADR-017：web_search 关键词搜索工具（DuckDuckGo Lite 免 key）
- **日期**：2026-08-30
- **状态**：已实施
- **背景**：web_fetch 解决"给定 URL 抓取"，但 agent 仍需知道抓哪个 URL；面对陌生主题只能凭记忆猜 URL。业界标配 WebSearch 作发现前置。
- **选项**：DuckDuckGo Lite 免 key（HTML 解析）/ 搜索 API（需新 key）/ Bing HTML 抓取（重定向 URL 难还原）。
- **决策**：新增 `web.py` `search()` + `parse_search_results()`（纯函数）+ `web_search` 工具；后端固定 `lite.duckduckgo.com/lite/`（复用代理感知 + 逐跳公网校验 + 限量链路，零新 SSRF 面）；返回编号标题+真实 URL（uddg 解码，仅 http(s)）+摘要（≤200 字符）；默认 max_results=8 clamp 1..10；失败重试 3 次；session 可注入离线测试。
- **理由**：零新依赖零新凭据、契合"重要逻辑自实现"；结果 URL 由 web_fetch 自带 SSRF 防护，形成"搜索→抓取→查证"闭环。
- **影响**：TOOL_SCHEMAS 增至 9 个；SYSTEM_PROMPT 注入"先 search 发现 URL、再 web_fetch 抓详情"规则；与 ADR-016 同住 web.py。
```

- [ ] **Step 6: README.md**

- 功能特性第一条：工具数量 8 → 9，追加 `web_search`（DuckDuckGo Lite 免 key，返回标题/URL/摘要）。
- 项目结构 web.py 行更新（含 search）。
- 测试计数更新到实际值。

- [ ] **Step 7: 运行全量测试 + 提交**

Run: `uv run pytest tests/ -q`
Expected: 全绿（实际总数以运行结果为准，若文档中需写数，用实际值）
```bash
git add code_agent/docs/ README.md
git commit -m "docs: 同步 web_search 文档并记录 ADR-017（架构/工具/设计/开发/README）"
```

---

### Task 6: 全量回归 + 真实 API 冒烟 + 凭据复核

**Files:**
- 无（验证与收尾）。

**Interfaces:**
- Consumes: Task 1-5 全部实现。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全绿

- [ ] **Step 2: CLI --help 正常**

Run: `uv run python -m code_agent --help`
Expected: 正常输出

- [ ] **Step 3: 真实 API 冒烟**

经用户确认后运行（任务示例：搜索→抓取→查证闭环）：

```bash
uv run python -m code_agent --workdir /home/kiki/workspace/code_agent_project/test_workspace --prompt "用 web_search 搜索 'python requests http library' 的最新官方文档地址，再用 web_fetch 抓取其中一条确认，报告带来源的结果"
```

Expected: agent 自主调用 `web_search` → 选一条结果 URL → `web_fetch` 抓详情 → 报告带来源事实。

- [ ] **Step 4: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中

- [ ] **Step 5: 收尾确认**

- 工具权威文档（tools.md）与代码同步；`test_tools.py` schema 断言为 9 个。
- `git status` 干净；提交历史完整（本迭代 6 个 commit + 可能修复波，未改写历史）。
