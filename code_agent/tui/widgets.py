"""Custom widgets for the code_agent TUI."""
from __future__ import annotations

from code_agent.tui import _line_style
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option
from rich.text import Text


class StatusBar(Widget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._text = Text("idle", style="green")

    def update_status(self, state: str, model: str = "", session_id: str = "", workspace_line: str = "") -> None:
        color = "green" if state == "idle" else "yellow"
        parts = []
        if workspace_line:
            parts.append(workspace_line)
        if model:
            parts.append(f"model: {model}")
        if session_id:
            parts.append(f"session: {session_id}")
        head = " | ".join(parts)
        dot = "●"
        self._text = Text()
        self._text.append(head + ("  " if head else "") + dot + " ", style="default")
        self._text.append(state, style=color)
        self.refresh()

    def render(self) -> Text:
        return self._text


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

    def set_ask_mode(self, prompt: str) -> None:
        self.set_class(False, "command-mode")
        self._ask_mode = True
        self.value = ""
        self.placeholder = prompt

    def clear_ask_mode(self) -> None:
        self._ask_mode = False
        self.placeholder = "❯ 输入任务（/ 开头为命令）"

    def on_input_changed(self, event) -> None:
        if self._ask_mode:
            return
        if self.value.startswith("!"):
            self.set_class(True, "command-mode")
            self.placeholder = "❯ shell: 输入命令（回车执行）"
        else:
            self.set_class(False, "command-mode")
            self.placeholder = "❯ 输入任务（/ 开头为命令）"
