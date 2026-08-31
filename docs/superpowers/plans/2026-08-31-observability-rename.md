# TUI 可观测性（token/上下文/缓存率）+ 会话重命名 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 TUI 状态栏常驻显示真实 token 占用/缓存命中率、自动解析上下文窗口并收紧裁剪预算，并支持 Ctrl+R / `/rename` 固定会话标题。

**Architecture:** 沿现有回调管道（方案 A）：`llm.py` 解析末 chunk usage 并新增 `resolve_context_window`；`agent.py` 计算预算 B、暴露 `last_usage`/`context_window`、`run_task` 新增 `on_stats` 回调、新增 `rename_session`；`session.py` 加 `rename`（pin）；`cli.py` 加 `--context-window` 与 `/rename`；`tui/` 的 StatusBar 渲染 ctx/cache、PromptInput 加 rename 模式、Ctrl+R 接线。

**Tech Stack:** Python 3.11+，Textual（tui），pytest（离线测试），requests（/models 查询）。

## Global Constraints

- 所有命令在 `code_agent/` 目录内经 `uv run` 执行（`uv run pytest ...`、`uv run python -m code_agent ...`）。
- 测试全离线：真实 API key 一律不用；网络相关逻辑用注入的 `get_json` / monkeypatch。
- 凭据不入库、不入文档、不入提交信息；提交前 `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"` 无命中。
- 预算语义固定：`B = min(CLI --max-context-tokens, int(0.7 × W))`，W 为上下文窗口；`--max-context-tokens` 默认 90000。
- 展示分母 = 预算 B；真实 usage 优先，缺失时启发式回退并加 `~` 前缀。
- 手动重命名后标题 pin 固定，不再被自动标题覆盖。
- 无无关重构；本计划只改本 spec 覆盖的文件。

---

### Task 1: SessionStore rename + 标题 pin

**Files:**
- Modify: `code_agent/code_agent/session.py:75-84`（`save` 尊重 pin）
- Modify: `code_agent/code_agent/session.py:111-115`（追加 `_write_lines`）
- Test: `code_agent/tests/test_session.py`

**Interfaces:**
- Consumes: 既有 `SessionStore._read_meta` / `_write`、`datetime`。
- Produces:
  - `SessionStore.rename(session_id: str, title: str) -> None`（meta 缺失抛 `KeyError`；保留消息行，置 `title` + `title_pinned: true` + 更新 `updated_at`）。
  - `SessionStore.save(session_id, messages, title=None)` 新语义：existing pinned → 保持现标题；否则 title 生效。
  - 私有 `SessionStore._write_lines(path: str, lines: list[str]) -> None`（原子写，无末尾换行）。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_session.py`）

```python
def test_rename_pins_title(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("auto")
    store.save(sid, [{"role": "user", "content": "u"}], title="auto2")
    store.rename(sid, "manual title")
    store.save(sid, [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}], title="ignored")
    meta, msgs = store.load(sid)
    assert meta["title"] == "manual title"
    assert meta.get("title_pinned") is True
    assert len(msgs) == 2


def test_rename_keeps_created_at_and_messages(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    store.save(sid, [{"role": "user", "content": "u"}, {"role": "tool", "tool_call_id": "x", "name": "n", "content": "c"}])
    before, _ = store.load(sid)
    store.rename(sid, "renamed")
    meta, msgs = store.load(sid)
    assert meta["created_at"] == before["created_at"]
    assert meta["updated_at"] >= before["updated_at"]
    assert len(msgs) == 2
    assert msgs[0]["content"] == "u"


def test_rename_missing_raises_keyerror(tmp_path):
    store = SessionStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.rename("code_agent-nope", "x")


def test_save_unpinned_updates_title(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t1")
    store.save(sid, [{"role": "user", "content": "a"}], title="t2")
    store.save(sid, [{"role": "user", "content": "b"}], title="t3")
    meta, _ = store.load(sid)
    assert meta["title"] == "t3"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_session.py -v`
Expected: 新用例报 `AttributeError: 'SessionStore' object has no attribute 'rename'`；`test_rename_pins_title` 因 `save` 尚不尊重 pin 而失败（title 被覆盖为 "ignored"）。

- [ ] **Step 3: 实现**

在 `session.py` 的 `save` 方法中替换标题解析逻辑：

```python
    def save(self, session_id: str, messages: list[dict], title: str | None = None) -> None:
        path = self._path(self.root, session_id)
        existing = self._read_meta(path) if os.path.isfile(path) else None
        now = datetime.now().isoformat(timespec="microseconds")
        created_at = existing["created_at"] if existing else now
        if existing is None:
            os.makedirs(self.root, exist_ok=True)
        if existing is not None and existing.get("title_pinned"):
            resolved_title = existing.get("title") or ""
        elif title is not None:
            resolved_title = title
        else:
            resolved_title = (existing.get("title") or "") if existing else ""
        meta = _meta_dict(session_id, resolved_title, created_at, now, len(messages))
        if existing is not None and existing.get("title_pinned"):
            meta["title_pinned"] = True
        self._write(path, [meta] + list(messages))
```

在 `session.py` 的 `_write` 后追加 `_write_lines` 与 `rename`：

```python
    def _write_lines(self, path: str, lines: list[str]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)

    def rename(self, session_id: str, title: str) -> None:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            raise KeyError(session_id)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        meta_line = lines[0] if lines else ""
        try:
            meta = json.loads(meta_line)
        except json.JSONDecodeError:
            raise KeyError(session_id) from None
        if not isinstance(meta, dict) or meta.get("type") != "meta":
            raise KeyError(session_id)
        meta["title"] = title
        meta["title_pinned"] = True
        meta["updated_at"] = datetime.now().isoformat(timespec="microseconds")
        lines[0] = json.dumps(meta, ensure_ascii=False)
        self._write_lines(path, lines)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_session.py -v`
Expected: 全部 PASS（原有用例不受影响，`test_save_keeps_created_at` 等照常）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/session.py tests/test_session.py
git commit -m "feat: SessionStore.rename + 标题 pin 固定（可观测性/重命名 Task 1/7）"
```

---

### Task 2: llm.py Usage 解析 + include_usage 兜底 + resolve_context_window

**Files:**
- Modify: `code_agent/code_agent/llm.py`
- Test: `code_agent/tests/test_llm_parse.py`

**Interfaces:**
- Consumes: 既有 `_StreamAccumulator` / `_request` / `LLMResponse`。
- Produces:
  - `Usage(prompt_tokens: int, completion_tokens: int = 0, total_tokens: int = 0, cached_tokens: int = 0, heuristic: bool = False)` dataclass。
  - `LLMResponse.content / tool_calls / usage: Usage | None = None`。
  - `parse_usage(data: dict | None) -> Usage | None`（无效/缺 prompt_tokens → None）。
  - `resolve_context_window(model, base_url="", api_key="", *, get_json=None, default=1_000_000) -> int`。
  - `LLMClient(..., use_usage: bool = True)`；`chat()` 带 `stream_options.include_usage`，LLMError 时去掉字段重试一次。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_llm_parse.py`）

```python
from code_agent.llm import LLMClient, LLMError, LLMResponse, Usage, resolve_context_window


def test_accumulator_captures_usage():
    acc = _StreamAccumulator()
    acc.feed({"content": "hi"})
    acc.feed({"usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                        "prompt_tokens_details": {"cached_tokens": 40}}})
    resp = acc.result()
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 100
    assert resp.usage.cached_tokens == 40
    assert resp.usage.heuristic is False


def test_accumulator_usage_none_when_missing():
    acc = _StreamAccumulator()
    acc.feed({"content": "x"})
    assert acc.result().usage is None


def test_accumulator_usage_cached_default_zero():
    acc = _StreamAccumulator()
    acc.feed({"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    u = acc.result().usage
    assert u.cached_tokens == 0


def test_chat_keeps_include_usage_when_accepted(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        return LLMResponse(content="ok", tool_calls=[])

    monkeypatch.setattr(client, "_request", fake_request)
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert len(calls) == 1
    assert calls[0]["stream_options"] == {"include_usage": True}


def test_chat_falls_back_without_include_usage(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        if len(calls) == 1:
            raise LLMError("HTTP 400: unknown field stream_options")
        return LLMResponse(content="ok", tool_calls=[])

    monkeypatch.setattr(client, "_request", fake_request)
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in calls[1]


def test_chat_propagates_error_when_retry_still_fails(monkeypatch):
    client = LLMClient(base_url="https://x/v1", api_key="k", model="m", max_retries=3)
    calls = []

    def fake_request(payload, headers, on_delta):
        calls.append(payload)
        raise LLMError("HTTP 401: bad key")

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2  # 首带 include_usage，回退一次仍失败


def test_resolve_context_window_from_models():
    def get_json(url, api_key):
        assert url.endswith("/models")
        return {"data": [{"id": "custom-model", "context_length": 42000}]}

    assert resolve_context_window("custom-model", "https://api.example.com/v1", "k", get_json=get_json) == 42000


def test_resolve_context_window_strips_chat_completions():
    def get_json(url, api_key):
        return {"data": [{"id": "custom", "context_length": 42000}]}

    assert resolve_context_window("custom", "https://api.example.com/v1/chat/completions", "k", get_json=get_json) == 42000


def test_resolve_context_window_table_fallback():
    assert resolve_context_window("deepseek-chat", "", "") == 64000
    assert resolve_context_window("gpt-4o-2024-08-06", "", "") == 128000


def test_resolve_context_window_default():
    assert resolve_context_window("unknown-model", "", "") == 1_000_000


def test_resolve_context_window_network_failure_silent():
    def get_json(url, api_key):
        raise RuntimeError("boom")

    assert resolve_context_window("anything", "https://x/v1", "k", get_json=get_json) == 1_000_000
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_llm_parse.py -v`
Expected: 新用例报错（`Usage`/`resolve_context_window` 不存在、`LLMResponse` 无 `usage` 属性等）。

- [ ] **Step 3: 实现**

在 `llm.py` 顶部 import 处加 `import os`。在 `ToolCall` 后新增：

```python
@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    heuristic: bool = False


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage | None = None


def _nonneg_int(v) -> int:
    return v if isinstance(v, int) and v >= 0 else 0


def parse_usage(data: dict | None) -> Usage | None:
    if not isinstance(data, dict):
        return None
    prompt = data.get("prompt_tokens")
    if not isinstance(prompt, int) or prompt < 0:
        return None
    details = data.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=_nonneg_int(data.get("completion_tokens")),
        total_tokens=_nonneg_int(data.get("total_tokens")),
        cached_tokens=_nonneg_int(details.get("cached_tokens")),
    )
```

替换 `_StreamAccumulator`：给 `result()` 返回 `usage`，`feed()` 捕获 usage（在 `content` 拼接之后追加）：

```python
@dataclass
class _StreamAccumulator:
    content: str = ""
    _calls: dict[int, dict[str, str]] = field(default_factory=dict)
    usage: dict | None = None

    def feed(self, delta: dict) -> None:
        if isinstance(delta.get("content"), str):
            self.content += delta["content"]
        if isinstance(delta.get("usage"), dict):
            self.usage = delta["usage"]
        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def result(self) -> LLMResponse:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            calls.append(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["arguments"]),
                )
            )
        return LLMResponse(content=self.content, tool_calls=calls, usage=parse_usage(self.usage))
```

在 `_request` 的 SSE 循环中，跳过空 choices 前先捕获 usage（替换 `if not choices:` 之前的段落）：

```python
            for payload_line in iter_sse_lines(resp):
                if not payload_line:
                    continue
                try:
                    chunk = json.loads(payload_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    acc.usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                acc.feed(delta)
                content = delta.get("content")
                if isinstance(content, str) and on_delta:
                    on_delta(content)
```

`LLMClient.__init__` 增加参数（在 `debug` 后）：

```python
        use_usage: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug
        self.use_usage = use_usage
```

替换 `chat`（加 include_usage 与 LLMError 回退）：

```python
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        include_usage = self.use_usage
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
            if tools:
                payload["tools"] = tools
            if include_usage:
                payload["stream_options"] = {"include_usage": True}
            try:
                return self._request(payload, headers, on_delta)
            except _Retryable as e:
                last_error = e
            except (requests.RequestException, OSError) as e:
                last_error = e
            except LLMError as e:
                last_error = e
                if include_usage:
                    include_usage = False  # 严格网关可能拒该字段，去掉重试一次
                    continue
                raise
            if attempt < self.max_retries - 1:
                if self.debug:
                    print(f"[llm] attempt {attempt + 1} failed: {last_error}; retrying in {2 ** attempt}s")
                time.sleep(2 ** attempt)
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")
```

在 `parse_tool_arguments` 之后新增上下文窗口解析：

```python
_MODEL_CONTEXT_WINDOW_PREFIXES = [
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1-mini", 128_000),
    ("gpt-4.1", 128_000),
    ("o1-mini", 128_000),
    ("o1", 200_000),
    ("o3-mini", 200_000),
    ("o3", 200_000),
    ("deepseek-chat", 64_000),
    ("deepseek-reasoner", 64_000),
]


def _get_json(url: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _table_context_window(model: str) -> int | None:
    for prefix, size in _MODEL_CONTEXT_WINDOW_PREFIXES:
        if model.startswith(prefix):
            return size
    return None


def resolve_context_window(
    model: str,
    base_url: str = "",
    api_key: str = "",
    *,
    get_json: Callable[[str, str], dict] | None = None,
    default: int = 1_000_000,
) -> int:
    """Resolve model context window: /models → name table → default. Never raises."""
    root = (base_url or "").rstrip("/")
    if root.endswith("/chat/completions"):
        root = root[: -len("/chat/completions")]
    if root:
        fetcher = get_json or _get_json
        try:
            data = fetcher(f"{root}/models", api_key)
            for m in data.get("data") or []:
                if isinstance(m, dict) and m.get("id") == model and isinstance(m.get("context_length"), int):
                    return m["context_length"]
        except Exception:
            pass  # 网络/解析失败走下一级
    size = _table_context_window(model)
    if size is not None:
        return size
    return default
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_llm_parse.py tests/test_cli.py -v`
Expected: 全部 PASS（`test_cli.py` 回归确认 `_request` 改动未破坏 `_make_client`）。

- [ ] **Step 5: 提交**

```bash
git add code_agent/llm.py tests/test_llm_parse.py
git commit -m "feat: usage 解析 + include_usage 兜底 + resolve_context_window（可观测性/重命名 Task 2/7）"
```

---

### Task 3: agent.py 预算/on_stats/启发式回退/rename_session

**Files:**
- Modify: `code_agent/code_agent/agent.py`
- Test: `code_agent/tests/test_agent.py`

**Interfaces:**
- Consumes: `Usage`/`LLMResponse`（Task 2）、`estimate_tokens`（`code_agent.context`）、`SessionStore.rename`（Task 1）。
- Produces:
  - `AgentSession(..., context_window: int | None = None)`；属性 `context_window: int`、`max_context_tokens: int`（= B）、`last_usage: Usage | None`。
  - `run_task(..., on_stats: Callable[[Usage], None] | None = None)`：每回合 chat 成功后回调（含启发式回退标记）。
  - `AgentSession.rename_session(title: str) -> str`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_agent.py`）

```python
from code_agent.llm import LLMResponse, Usage


def test_context_window_budget_64k(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object(), max_context_tokens=90000, context_window=64000)
    assert s.max_context_tokens == 44800
    assert s.context_window == 64000


def test_context_window_budget_128k(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object(), max_context_tokens=90000, context_window=128000)
    assert s.max_context_tokens == 89600


def test_context_window_defaults(workdir):
    from code_agent.agent import AgentSession
    s = AgentSession(workdir=workdir, llm=object())
    assert s.max_context_tokens == 90000
    assert s.context_window == 1_000_000


def test_run_task_on_stats_heuristic_fallback(workdir):
    from code_agent.agent import AgentSession

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[])

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    got = []
    res = s.run_task("task", on_stats=got.append)
    assert res.finished is True
    assert len(got) == 1
    assert got[0].heuristic is True
    assert got[0].prompt_tokens > 0
    assert s.last_usage is got[0]


def test_run_task_on_stats_real_usage(workdir):
    from code_agent.agent import AgentSession

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=123, completion_tokens=4, cached_tokens=50))

    s = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=2)
    got = []
    s.run_task("task", on_stats=got.append)
    assert got[0].prompt_tokens == 123
    assert got[0].cached_tokens == 50
    assert got[0].heuristic is False


def test_rename_session_creates_then_pins(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store)
    assert s.session_id is None
    title = s.rename_session("my title")
    assert title == "my title"
    assert s.session_id is not None
    meta, _ = store.load(s.session_id)
    assert meta["title"] == "my title"
    assert meta.get("title_pinned") is True


def test_rename_session_existing_id(tmp_path):
    from code_agent.agent import AgentSession
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("auto")
    s = AgentSession(workdir=str(tmp_path), llm=object(), store=store, session_id=sid)
    s.rename_session("new name")
    meta, _ = store.load(sid)
    assert meta["title"] == "new name"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 新用例失败（`context_window` 参数不存在、`on_stats` 不被调用、`rename_session` 不存在）。

- [ ] **Step 3: 实现**

`agent.py` 顶部 import 修改（把 `from code_agent.context import Conversation` 与 `from code_agent.llm import LLMError` 替换为）：

```python
from code_agent.context import Conversation, estimate_tokens
from code_agent.llm import LLMError, Usage
```

`AgentSession.__init__` 签名增加参数并计算预算（在 `allow_subagent` 参数后追加，并在 `self.allow_subagent = allow_subagent` 后插入两行）：

```python
        allow_subagent: bool = True,
        context_window: int | None = None,
    ) -> None:
```

```python
        self.allow_subagent = allow_subagent
        self.last_usage: Usage | None = None
        self.context_window = context_window if context_window else 1_000_000
        if context_window:
            self.max_context_tokens = min(max_context_tokens, int(0.7 * context_window))
```

`run_task` 签名增加参数（在 `on_tool_start` 参数后追加）：

```python
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_stats: Callable[[Usage], None] | None = None,
    ) -> RunResult:
```

在 `llm_error_count = 0` 之后、`self.conversation.add_assistant(...)` 之前插入：

```python
                llm_error_count = 0
                if response.usage is not None:
                    usage = response.usage
                else:
                    usage = Usage(
                        prompt_tokens=sum(
                            estimate_tokens(str(m.get("content", ""))) for m in messages
                        ),
                        heuristic=True,
                    )
                self.last_usage = usage
                if on_stats is not None:
                    on_stats(usage)
                self.conversation.add_assistant(response.content, response.tool_calls or None)
```

`_run_tool` 之后、`_use_skill` 之前新增：

```python
    def rename_session(self, title: str) -> str:
        if self.store is None:
            raise ValueError("no session store configured")
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        if self.session_id is None:
            self.session_id = self.store.create(title)
        self.store.rename(self.session_id, title)
        return title
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_agent.py tests/test_cli.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/agent.py tests/test_agent.py
git commit -m "feat: AgentSession 上下文窗口预算/on_stats/启发式回退/rename_session（可观测性/重命名 Task 3/7）"
```

---

### Task 4: cli.py --context-window + /rename

**Files:**
- Modify: `code_agent/code_agent/cli.py`
- Test: `code_agent/tests/test_cli.py`

**Interfaces:**
- Consumes: `resolve_context_window`（Task 2）、`AgentSession.rename_session`（Task 3）。
- Produces: CLI `--context-window <n>`；`handle_command` 支持 `/rename <title>`。

- [ ] **Step 1: 写失败测试**（修改 + 追加到 `tests/test_cli.py`）

在 `tests/test_cli.py` 顶部 import 区后（`import os` / `import pytest` 之后）新增 autouse fixture（覆盖所有 main() 测试，避免离线测试联网）：

```python
@pytest.fixture(autouse=True)
def _no_context_window_resolve(monkeypatch):
    monkeypatch.setattr("code_agent.cli.resolve_context_window", lambda *a, **k: 128000)
```

在 `test_parser_defaults` 中追加断言：

```python
    assert args.context_window is None
```

追加新用例：

```python
def test_main_passes_context_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path), "--context-window", "64000"])
    assert rc == 0
    assert captured.get("context_window") == 64000


def test_handle_command_rename(workdir, tmp_path):
    from code_agent.cli import handle_command
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("auto")

    class _S:
        def __init__(self):
            self.session_id = sid
            self.called = None

        def rename_session(self, title):
            self.called = title
            return title

    s = _S()
    keep, out = handle_command("/rename my title", s, store)
    assert keep is True and s.called == "my title" and out == ["renamed: my title"]
    keep, out = handle_command("/rename", s, store)
    assert keep is True and out == ["usage: /rename <title>"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py -v`
Expected: `test_parser_defaults` 断言失败（`context_window` 属性不存在）、`test_main_passes_context_window` 捕获不到 `context_window`、`test_handle_command_rename` 输出与预期不符。

- [ ] **Step 3: 实现**

`cli.py` 顶部 import 改为：

```python
from code_agent.agent import AgentSession
from code_agent.llm import LLMClient, resolve_context_window
```

`_build_parser` 在 `--max-context-tokens` 后追加：

```python
    parser.add_argument("--context-window", type=int, default=None,
                        help="Model context window in tokens (default: auto-detect)")
```

`main` 中 `llm = _make_client(args)` 之后、`policy = ...` 之前插入：

```python
    llm = _make_client(args)
    if args.context_window:
        window = args.context_window
    else:
        window = resolve_context_window(llm.model, llm.base_url, llm.api_key)
```

`AgentSession(...)` 构造中 `max_context_tokens=args.max_context_tokens,` 之后追加：

```python
            context_window=window,
```

`handle_command` 中 `/resume` 分支之后、`/exit` 之前插入：

```python
    elif cmd == "/rename":
        title = parts[1] if len(parts) > 1 else ""
        if not title.strip():
            out.append("usage: /rename <title>")
        else:
            try:
                renamed = session.rename_session(title)
                out.append(f"renamed: {renamed}")
            except (KeyError, ValueError) as e:
                out.append(f"rename failed: {e}")
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/cli.py tests/test_cli.py
git commit -m "feat: CLI --context-window + /rename 命令（可观测性/重命名 Task 4/7）"
```

---

### Task 5: StatusBar usage 渲染 + PromptInput rename 模式

**Files:**
- Modify: `code_agent/code_agent/tui/widgets.py`
- Test: `code_agent/tests/test_tui_widgets.py`

**Interfaces:**
- Consumes: `Usage`（duck typing，无需 import）。
- Produces:
  - `_fmt_k(n: int) -> str`（1000+ → `12.3k`/`90k`，<1000 → 原数字）。
  - `_usage_segments(usage, context_window, *, compact=False) -> (ctx_seg: str, cache_seg: str)`。
  - `_status_width() -> int`。
  - `StatusBar.update_status(state, model="", session_id="", workspace_line="", usage=None, context_window=0)`。
  - `PromptInput.set_rename_mode() / clear_rename_mode()`、`_rename_mode` 标志。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_tui_widgets.py`）

```python
from code_agent.llm import Usage
from code_agent.tui.widgets import _usage_segments, StatusBar


def test_usage_segments_full():
    ctx, cache = _usage_segments(Usage(prompt_tokens=12340, cached_tokens=5000), 90000)
    assert ctx == "ctx 12.3k/90k 13%"
    assert cache == "cache 40%"


def test_usage_segments_heuristic_prefix_and_no_cache():
    ctx, cache = _usage_segments(Usage(prompt_tokens=1000, heuristic=True), 90000)
    assert ctx == "ctx ~1k/90k 1%"
    assert cache == ""


def test_usage_segments_none():
    assert _usage_segments(None, 90000) == ("", "")


def test_usage_segments_zero_cache_omitted():
    ctx, cache = _usage_segments(Usage(prompt_tokens=5000, cached_tokens=0), 90000)
    assert "cache" not in cache
    assert ctx == "ctx 5k/90k 5%"


def test_usage_segments_compact_drops_pct_and_cache():
    ctx, cache = _usage_segments(Usage(prompt_tokens=12340, cached_tokens=5000), 90000, compact=True)
    assert ctx == "ctx 12.3k/90k"
    assert cache == ""


def test_status_bar_renders_usage(monkeypatch):
    monkeypatch.setattr("code_agent.tui.widgets._status_width", lambda: 200)
    sb = StatusBar()
    sb.update_status("idle", model="m", session_id="s1",
                     workspace_line="Workspace: w", usage=Usage(prompt_tokens=12000, cached_tokens=3000),
                     context_window=90000)
    plain = sb.render().plain
    assert "ctx 12k/90k 13%" in plain
    assert "cache 25%" in plain


def test_status_bar_trims_pct_when_narrow(monkeypatch):
    monkeypatch.setattr("code_agent.tui.widgets._status_width", lambda: 30)
    sb = StatusBar()
    sb.update_status("idle", model="m", session_id="s1",
                     workspace_line="Workspace: w", usage=Usage(prompt_tokens=12000),
                     context_window=90000)
    plain = sb.render().plain
    assert "12k/90k" in plain
    assert "%" not in plain
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_widgets.py -v`
Expected: `_usage_segments`/`_status_width` 不存在、StatusBar 无 usage 渲染。

- [ ] **Step 3: 实现**

`widgets.py` 顶部 import 追加：

```python
import shutil
```

在 `StatusBar` 类之前新增纯函数：

```python
def _fmt_k(n: int) -> str:
    if n < 1000:
        return str(n)
    s = f"{n / 1000:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s + "k"


def _usage_segments(usage, context_window, *, compact: bool = False) -> tuple[str, str]:
    if usage is None:
        return "", ""
    prompt = usage.prompt_tokens
    denom = context_window or prompt
    prefix = "~" if usage.heuristic else ""
    if compact:
        return f"ctx {prefix}{_fmt_k(prompt)}/{_fmt_k(denom)}", ""
    pct = int(prompt / denom * 100) if denom else 0
    warn = "!" if prompt > denom else ""
    ctx = f"ctx {prefix}{_fmt_k(prompt)}/{_fmt_k(denom)} {pct}%{warn}"
    cache = ""
    if not usage.heuristic and prompt:
        if usage.cached_tokens:
            cache = f"cache {int(usage.cached_tokens / prompt * 100)}%"
    return ctx, cache


def _status_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80
```

替换 `StatusBar.update_status`：

```python
    def update_status(self, state: str, model: str = "", session_id: str = "",
                      workspace_line: str = "", usage=None, context_window: int = 0) -> None:
        color = "green" if state == "idle" else "yellow"
        ctx, cache = _usage_segments(usage, context_window)
        parts = []
        if workspace_line:
            parts.append(workspace_line)
        if model:
            parts.append(f"model: {model}")
        if session_id:
            parts.append(f"session: {session_id}")
        if ctx:
            parts.append(ctx)
        if cache:
            parts.append(cache)
        head = " | ".join(parts)
        if len(head) > _status_width():
            ctx_c, _cache_c = _usage_segments(usage, context_window, compact=True)
            parts = [p for p in parts if not (p.startswith("ctx ") or p.startswith("cache "))]
            if ctx_c:
                parts.append(ctx_c)
            head = " | ".join(parts)
        dot = "●"
        self._text = Text()
        self._text.append(head + ("  " if head else "") + dot + " ", style="default")
        self._text.append(state, style=color)
        self.refresh()
```

`PromptInput.__init__` 加标志：

```python
        self._ask_mode = False
        self._rename_mode = False
```

在 `clear_ask_mode` 后新增：

```python
    def set_rename_mode(self) -> None:
        self.set_class(False, "command-mode")
        self._rename_mode = True
        self.value = ""
        self.placeholder = "❯ 输入新会话名（回车确认，Esc 取消）"

    def clear_rename_mode(self) -> None:
        self._rename_mode = False
        self.placeholder = "❯ 输入任务（/ 开头为命令）"
```

`on_input_changed` 首行守卫改为：

```python
    def on_input_changed(self, event) -> None:
        if self._ask_mode or self._rename_mode:
            return
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_widgets.py tests/test_tui.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/tui/widgets.py tests/test_tui_widgets.py
git commit -m "feat: StatusBar ctx/cache 渲染 + PromptInput rename 模式（可观测性/重命名 Task 5/7）"
```

---

### Task 6: TUI Ctrl+R rename + on_stats 桥

**Files:**
- Modify: `code_agent/code_agent/tui/app.py`
- Modify: `code_agent/code_agent/tui/worker.py`
- Test: `code_agent/tests/test_tui_app.py`
- Test: `code_agent/tests/test_tui_worker.py`

**Interfaces:**
- Consumes: `AgentSession.rename_session`/`last_usage`/`context_window`（Task 3）、`_usage_segments`（Task 5）、`AgentWorker.on_stats`（本任务）。
- Produces:
  - `CodeAgentApp.BINDINGS` 含 `ctrl+r → rename_session`、`escape → cancel_rename`（show=False）。
  - `CodeAgentApp.action_rename_session / action_cancel_rename / _finish_rename`。
  - `AgentWorker(..., on_stats=None)` 桥接 `_stats` 到 `run_task(on_stats=...)`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tui_app.py`：

```python
def test_app_rename_session(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            inp.value = "我的会话"
            await pilot.press("enter")
            assert session.session_id is not None
            meta, _ = store.load(session.session_id)
            assert meta["title"] == "我的会话"
            assert meta.get("title_pinned") is True
            assert inp._rename_mode is False
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_rename_esc_cancels(workdir, tmp_path):
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    session = AgentSession(workdir=workdir, llm=_FakeLLM(), max_iterations=3, store=store)
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            await pilot.press("ctrl+r")
            assert inp._rename_mode is True
            await pilot.press("escape")
            assert inp._rename_mode is False
            assert session.session_id is None
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_app_on_stats_updates_status(workdir, tmp_path):
    from code_agent.llm import Usage
    from code_agent.tui.widgets import StatusBar
    from code_agent.tui.worker import AgentWorker

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=12000, cached_tokens=3000))

    session = AgentSession(workdir=workdir, llm=_LLM(), max_iterations=3, context_window=90000)
    store = SessionStore(str(tmp_path / "sessions"))
    app = CodeAgentApp(session, store, None, model="test")

    async def scenario():
        async with app.run_test() as pilot:
            inp = app.query_one("#input")
            inp.value = "hi"
            await pilot.press("enter")
            for _ in range(80):
                sb = app.query_one("#status", StatusBar)
                if "ctx" in sb.render().plain:
                    break
                await asyncio.sleep(0.02)
            plain = sb.render().plain
            assert "ctx 12k/90k 13%" in plain
            assert "cache 25%" in plain
            await pilot.press("ctrl+q")

    asyncio.run(scenario())
```

追加到 `tests/test_tui_worker.py`：

```python
def test_worker_bridges_on_stats(workdir):
    from code_agent.llm import Usage
    from code_agent.tui.worker import AgentWorker

    class _LLM:
        def chat(self, messages, tools=None, on_delta=None):
            return LLMResponse(content="done", tool_calls=[],
                               usage=Usage(prompt_tokens=10, completion_tokens=2))

    session = _make_session(workdir, _LLM())
    app = _FakeApp()
    stats = []

    w = AgentWorker(
        app, session,
        on_delta=lambda c: None,
        on_tool=lambda n, r: None,
        on_done=lambda r: None,
        on_stats=lambda u: stats.append(u),
    )
    w.start("hi")
    deadline = time.time() + 5
    while not stats and time.time() < deadline:
        time.sleep(0.02)
    assert stats and stats[0].prompt_tokens == 10
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_tui_app.py tests/test_tui_worker.py -v`
Expected: `ctrl+r` 无绑定、`_rename_mode` 属性不存在、状态栏无 `ctx`、`AgentWorker` 不接受 `on_stats`。

- [ ] **Step 3: 实现**

`worker.py` 的 `AgentWorker.__init__` 签名（在 `on_tool_start` 后追加）：

```python
        on_tool_start=None, on_stats=None) -> None:
```

`__init__` 体末尾追加：

```python
        self._on_stats = on_stats
```

`_run` 中 `run_task(...)` 调用追加：

```python
            result = self.session.run_task(
                task,
                on_delta=self._delta,
                on_tool=self._tool,
                on_assistant_start=self._assistant_start,
                on_tool_start=self._tool_start,
                on_stats=self._stats,
            )
```

类末尾新增：

```python
    def _stats(self, usage) -> None:
        if self._on_stats is not None:
            self.app.call_from_thread(lambda: self._on_stats(usage))
```

`app.py`：
1. import 追加 `StatusBar` 到 widgets import 行：

```python
from code_agent.tui.widgets import ConversationLog, PromptInput, SessionList, SkillList, StatusBar
```

2. `BINDINGS` 追加（`ctrl+s` 之后）：

```python
        Binding("ctrl+r", "rename_session", "Rename"),
        Binding("escape", "cancel_rename", "Cancel", show=False),
```

3. `__init__` 中 `self._skill_name = ""` 后追加：

```python
        self._rename_active = False
```

4. `_refresh_status` 改为传 usage/context_window：

```python
    def _refresh_status(self, state: str) -> None:
        self.query_one("#status", StatusBar).update_status(
            state, model=self.model,
            session_id=self.session.session_id or "new",
            workspace_line=self._workspace_line(),
            usage=getattr(self.session, "last_usage", None),
            context_window=getattr(self.session, "context_window", 0),
        )
```

5. `on_input_submitted` 中，在 `if self._ask_responder is not None:` 块之后、`if value.startswith("/"):` 之前插入：

```python
        if self._rename_active:
            self._finish_rename(value)
            return
```

6. `_on_ask` 之后新增 rename 相关方法：

```python
    def action_rename_session(self) -> None:
        if self._busy():
            self.notify("agent 正在运行中，请稍候", severity="warning")
            return
        if self._ask_responder is not None:
            self.notify("请先处理权限询问", severity="warning")
            return
        self._rename_active = True
        inp = self.query_one("#input", PromptInput)
        inp.set_rename_mode()
        inp.focus()

    def action_cancel_rename(self) -> None:
        if not self._rename_active:
            return
        self._rename_active = False
        self.query_one("#input", PromptInput).clear_rename_mode()

    def _finish_rename(self, value: str) -> None:
        self._rename_active = False
        self.query_one("#input", PromptInput).clear_rename_mode()
        title = value.strip()
        if not title:
            self.notify("未输入会话名", severity="warning")
            return
        try:
            renamed = self.session.rename_session(title)
        except (KeyError, ValueError) as e:
            self.notify(f"rename failed: {e}", severity="error")
            return
        self.notify(f"renamed: {renamed}")
        self.query_one("#sessions", SessionList).refresh_from(self.store)
        self._refresh_status("idle")

    def _on_stats(self, usage) -> None:
        self._refresh_status("running")
```

7. `_start_task` 中 `AgentWorker(...)` 追加：

```python
            on_stats=self._on_stats,
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_tui_app.py tests/test_tui_worker.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_agent/tui/app.py code_agent/tui/worker.py tests/test_tui_app.py tests/test_tui_worker.py
git commit -m "feat: TUI Ctrl+R 重命名 + on_stats 桥接状态栏（可观测性/重命名 Task 6/7）"
```

---

### Task 7: 文档同步 + ADR-023

**Files:**
- Modify: `code_agent/docs/architecture.md`
- Modify: `code_agent/docs/context-management.md`
- Modify: `code_agent/docs/development.md`
- Modify: `code_agent/docs/design.md`
- Modify: `/home/kiki/workspace/code_agent_project/.agent/03-decisions.md`（ADR-023，不入库）

**Interfaces:**
- Consumes: Task 1-6 的实际接口。

- [ ] **Step 1: 更新 `code_agent/docs/architecture.md`**

- `llm.py` 行追加：`Usage` / `LLMResponse.usage` / `resolve_context_window()`（/models→查表→默认 1M）/ `LLMClient(use_usage=True)`。
- `agent.py` 行追加：`context_window` 属性、预算 B = min(CLI, 70%×W)、`run_task(on_stats)`、`last_usage`、`rename_session(title)`。
- `session.py` 行追加：`rename(session_id, title)`（pin）、`save` 尊重 pin。
- `cli.py` 行追加：`--context-window`、`/rename`。
- `tui/` 包行追加：StatusBar ctx/cache 渲染、Ctrl+R rename、`AgentWorker(on_stats)`。
- §2 数据流追加一句：每回合 chat 后 `on_stats(usage)` 回调 → StatusBar。

- [ ] **Step 2: 更新 `code_agent/docs/context-management.md`**

§4 预算段追加：`B = min(CLI --max-context-tokens, int(0.7 × W))`；W 由 `resolve_context_window`（/models → 模型名查表 → 默认 1M）解析，CLI `--context-window` 可覆盖；状态栏以 B 为分母展示最近回合 prompt_tokens 与缓存命中率（真实 usage 优先，启发式回退加 `~`）。

- [ ] **Step 3: 更新 `code_agent/docs/development.md`**

- §2 CLI 参数列表追加 `--context-window <n>`。
- TUI 行为段追加：状态栏 ctx/cache 常驻、Ctrl+R 重命名（Esc 取消）、`/rename <title>`。
- §3 测试目录说明追加 test_tui_widgets（_usage_segments/StatusBar）与重命名用例说明。

- [ ] **Step 4: 更新 `code_agent/docs/design.md`**

§6 功能范围勾选新增两项：
```
- [x] TUI 可观测性：状态栏常驻 token 占用/预算占比/缓存命中率（真实 usage 优先，启发式回退）；上下文窗口自动解析（/models→查表→1M）+ 预算 B=min(CLI, 70%×W)
- [x] 会话重命名：Ctrl+R / /rename，手动标题 pin 固定（不被自动标题覆盖）
```
§8 开发路线追加（20/21）对应两项。

- [ ] **Step 5: 追加 ADR-023 到 `.agent/03-decisions.md`**（在 `## 后续决策记录处` 之前插入）

```markdown
## ADR-023：TUI 可观测性 + 会话重命名
- **日期**：2026-08-31
- **状态**：已实施
- **背景**：TUI 无法感知上下文占用/缓存状态；会话标题无法手动命名（首个用户消息自动生成，每次保存覆盖）。
- **决策**：①状态栏常驻 ctx 占用/预算占比/缓存命中率，数据真实 usage（`stream_options.include_usage`）优先、provider 不支持回退启发式（`~` 前缀），分母=预算 B；②上下文窗口 W 由 `resolve_context_window`（/models `context_length` → 模型名查表 → 默认 1M）解析，预算 B=min(CLI `--max-context-tokens`, 70%×W)，CLI `--context-window` 覆盖；③会话重命名 Ctrl+R + `/rename <title>`，meta 加 `title_pinned`，手动标题固定不再被自动标题覆盖；`LLMClient` 增加 `use_usage`，严格网关拒 `include_usage` 时去掉重试一次。
- **理由**：数据管道沿既有回调（方案 A）最小侵入；真实 usage 比启发式准确且能显示缓存命中；窗口自动解析防长任务被 API 拒；pin 避免手动命名被冲掉。
- **影响**：llm/agent/session/cli/tui 五处改动；测试相应扩展（usage 解析、预算、pin、Ctrl+R、_usage_segments）。
```

- [ ] **Step 6: 提交**

```bash
git add docs/architecture.md docs/context-management.md docs/development.md docs/design.md
git commit -m "docs: 同步可观测性/重命名文档并记录 ADR-023"
```

---

### Task 8: 全量回归 + 凭据复核

**Files:** 无代码改动。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -v`
Expected: 全部 PASS（现有 273 + 新增 ≈ 305 用例）。

- [ ] **Step 2: CLI 冒烟**

Run: `uv run python -m code_agent --help`
Expected: 正常输出，含 `--context-window`。

- [ ] **Step 3: 凭据复核**

Run: `git grep -iE "sk-[a-zA-Z0-9]{10,}|api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9]{16,}"`
Expected: 无命中。

- [ ] **Step 4: 提交收尾（若无未提交改动则跳过）**

```bash
git status
git log --oneline -12
```
