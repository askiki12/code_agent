"""Custom widgets for the code_agent TUI."""
from __future__ import annotations

from code_agent.tui import _line_style
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option
from rich.text import Text


def _fmt_ctx(n: int) -> str:
    return f"{n / 1000:.1f}k"


def _footer_stats(usage, context_window) -> str:
    if usage is None:
        return ""
    prompt = usage.prompt_tokens
    denom = context_window or prompt
    pct = int(prompt / denom * 100) if denom else 0
    prefix = "~" if usage.heuristic else ""
    parts = [f"{prefix}{_fmt_ctx(prompt)}({pct}%)"]
    if not usage.heuristic and prompt and usage.cached_tokens:
        parts.append(f"cache:{int(usage.cached_tokens / prompt * 100)}%")
    return " ".join(parts)


class StatusBar(Widget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = Text("idle", style="green")

    def update_status(self, state: str, model: str = "", session_title: str = "",
                      workspace_line: str = "") -> None:
        color = "green" if state == "idle" else "yellow"
        parts = []
        if workspace_line:
            parts.append(workspace_line)
        if model:
            parts.append(f"model: {model}")
        parts.append(f"session: {session_title or 'new'}")
        head = " | ".join(parts)
        dot = "●"
        self._text = Text()
        self._text.append(head + ("  " if head else "") + dot + " ", style="default")
        self._text.append(state, style=color)
        self.refresh()

    def render(self) -> Text:
        return self._text


class StatusFooter(Footer):
    DEFAULT_CSS = "#footer-stats { dock: right; margin-right: 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stats = Static("", id="footer-stats")

    def compose(self):
        yield self._stats
        yield from super().compose()

    def update_stats(self, text: str) -> None:
        self._stats.update(text)


class ConversationLog(VerticalScroll):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lines: list[Text] = []

    def compose(self):
        yield Static(self._render_body(), id="body")

    def _render_body(self) -> Text:
        t = Text()
        for line in self._lines:
            t.append_text(line)
            t.append("\n")
        return t

    def _update_body(self) -> None:
        if not self.is_mounted:
            return
        body = self.query_one("#body", Static)
        body.update(self._render_body())
        if self._near_bottom():
            self.scroll_end(animate=False)

    def _near_bottom(self) -> bool:
        try:
            return self.scroll_offset.y >= self.max_scroll_y - 1
        except Exception:
            return True

    def append(self, text: str) -> None:
        self._lines.append(Text(text, style=_line_style(text)))
        self._update_body()

    def update_last(self, text: str) -> None:
        if self._lines:
            self._lines[-1] = Text(text, style=_line_style(text))
            self._update_body()
        else:
            self.append(text)

    def update_line(self, idx: int, text: str) -> None:
        if 0 <= idx < len(self._lines):
            self._lines[idx] = Text(text, style=_line_style(text))
            self._update_body()

    def clear(self) -> None:
        self._lines = []
        self._update_body()


class SessionList(OptionList):
    def refresh_from(self, store) -> None:
        self.clear_options()
        for s in store.list_sessions():
            title = s.get("title") or ""
            prompt = f"{s['id'][-12:]}  {title}"
            self.add_option(Option(prompt, id=s["id"]))


class SkillList(OptionList):
    def refresh_from(self, registry) -> None:
        self.clear_options()
        for s in registry.scan():
            self.add_option(Option(f"{s.name}  {s.description}", id=s.name))


class PromptInput(Input):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ask_mode = False
        self._rename_mode = False

    def set_ask_mode(self, prompt: str) -> None:
        self.set_class(False, "command-mode")
        self._ask_mode = True
        self.value = ""
        self.placeholder = prompt

    def clear_ask_mode(self) -> None:
        self._ask_mode = False
        self.placeholder = "❯ 输入任务（/ 开头为命令）"

    def set_rename_mode(self) -> None:
        self.set_class(False, "command-mode")
        self._rename_mode = True
        self.value = ""
        self.placeholder = "❯ 输入新会话名（回车确认，Esc 取消）"

    def clear_rename_mode(self) -> None:
        self._rename_mode = False
        self.placeholder = "❯ 输入任务（/ 开头为命令）"

    def on_input_changed(self, event) -> None:
        if self._ask_mode or self._rename_mode:
            return
        if self.value.startswith("!"):
            self.set_class(True, "command-mode")
            self.placeholder = "❯ shell: 输入命令（回车执行）"
        else:
            self.set_class(False, "command-mode")
            self.placeholder = "❯ 输入任务（/ 开头为命令）"
