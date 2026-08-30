"""CodeAgentApp: the Textual application for code_agent."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from code_agent.cli import handle_command
from code_agent.tui import format_assistant, format_tool, format_user
from code_agent.tui.widgets import ConversationLog, PromptInput, SessionList, StatusBar
from code_agent.tui.worker import AgentWorker


class CodeAgentApp(App):
    CSS = """
    #log { height: 1fr; border: round $accent; }
    #input { dock: bottom; height: 3; }
    #status { height: 1; }
    #sessions { width: 34; border: round $primary; display: none; }
    #sessions.visible { display: block; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New"),
        Binding("ctrl+l", "toggle_sessions", "Sessions"),
    ]

    def __init__(self, session, store, workspace=None, *, model: str = "") -> None:
        super().__init__()
        self.session = session
        self.store = store
        self.workspace = workspace
        self.model = model
        self._worker: AgentWorker | None = None
        self._ask_responder = None
        self._assistant_idx = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield StatusBar(id="status")
        with Horizontal():
            yield ConversationLog(id="log")
            yield SessionList(id="sessions")
        yield PromptInput(placeholder="❯ 输入任务（/ 开头为命令）", id="input")
        yield Footer()

    def _workspace_line(self) -> str:
        return self.workspace.display() if self.workspace is not None else ""

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _clear_ask(self) -> None:
        self._ask_responder = None
        self.query_one("#input", PromptInput).clear_ask_mode()

    def _refresh_status(self, state: str) -> None:
        self.query_one("#status", StatusBar).update_status(
            state, model=self.model,
            session_id=self.session.session_id or "new",
            workspace_line=self._workspace_line(),
        )

    def on_mount(self) -> None:
        self._refresh_status("idle")
        self.query_one("#sessions", SessionList).refresh_from(self.store)
        # 恢复会话时按历史填充对话区
        self._reload_conversation()
        # 默认聚焦输入框，便于直接回车提交任务
        self.query_one("#input", PromptInput).focus()

    def _reload_conversation(self) -> None:
        log = self.query_one("#log", ConversationLog)
        log.clear()
        for m in self.session.conversation.messages:
            role = m.get("role")
            content = str(m.get("content", ""))
            if role == "user":
                log.append(format_user(content))
            elif role == "assistant":
                log.append(format_assistant(content))
            elif role == "tool":
                log.append(f"[tool] {m.get('name', '')} | {content.splitlines()[0] if content else ''}")

    def on_input_submitted(self, event) -> None:
        value = event.value.strip()
        event.input.clear()
        if not value:
            return
        if self._ask_responder is not None:
            responder = self._ask_responder
            self._clear_ask()
            self.query_one("#log", ConversationLog).append(f"[permission] {value}")
            responder(value)
            return
        if value.startswith("/"):
            if self._busy():
                self.notify("agent 正在运行中，请稍候", severity="warning")
                return
            keep, out = handle_command(value, self.session, self.store)
            log = self.query_one("#log", ConversationLog)
            for line in out:
                log.append(line)
            if not keep:
                self.exit()
                return
            if value == "/new":
                log.clear()
                log.append("New session started.")
            self._refresh_status("idle")
            return
        if self._busy():
            self.notify("agent 正在运行中", severity="warning")
            return
        self._start_task(value)

    def _start_task(self, task: str) -> None:
        log = self.query_one("#log", ConversationLog)
        log.append(format_user(task))
        log.append("assistant: ")
        self._assistant_idx = len(log._lines) - 1
        self._refresh_status("running")
        self._worker = AgentWorker(
            self, self.session,
            on_delta=lambda c: self._on_delta(c),
            on_tool=lambda n, r: self._on_tool(n, r),
            on_done=lambda r: self._on_done(r),
            on_ask=lambda p, resp: self._on_ask(p, resp),
            on_ask_timeout=lambda: self._clear_ask(),
        )
        self._worker.start(task)

    def _on_delta(self, chunk: str) -> None:
        log = self.query_one("#log", ConversationLog)
        if 0 <= self._assistant_idx < len(log._lines):
            current = log._lines[self._assistant_idx].plain + chunk
            log.update_line(self._assistant_idx, current)

    def _on_tool(self, name, res) -> None:
        self.query_one("#log", ConversationLog).append(format_tool(name, res.ok, res.truncated, res.output))

    def _on_done(self, result) -> None:
        log = self.query_one("#log", ConversationLog)
        if result.final_text:
            log.update_line(self._assistant_idx, format_assistant(result.final_text))
        if not result.finished:
            log.append(f"[agent] stopped: {result.reason}")
        if self.session.session_id:
            log.append(f"[session {self.session.session_id}]")
        self._refresh_status("idle")

    def _on_ask(self, prompt: str, responder) -> None:
        self._ask_responder = responder
        self.query_one("#input", PromptInput).set_ask_mode(prompt)
        self.query_one("#input", PromptInput).focus()

    def action_new_session(self) -> None:
        if self._busy():
            self.notify("agent 正在运行中，请稍候", severity="warning")
            return
        self.session.new_session()
        log = self.query_one("#log", ConversationLog)
        log.clear()
        log.append("New session started.")
        self._refresh_status("idle")

    def action_toggle_sessions(self) -> None:
        sl = self.query_one("#sessions", SessionList)
        sl.toggle_class("visible")
        sl.refresh_from(self.store)

    def on_option_list_option_selected(self, event) -> None:
        if self._busy():
            self.notify("agent 正在运行中，请稍候", severity="warning")
            return
        sid = event.option.id
        if not sid:
            return
        try:
            self.session.load_session(sid)
        except KeyError:
            self.query_one("#log", ConversationLog).append(f"session not found: {sid}")
            return
        self._reload_conversation()
        self._refresh_status("idle")
