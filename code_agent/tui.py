"""Rich terminal UI for interactive mode."""
from __future__ import annotations

from code_agent.cli import handle_command
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def format_user(content: str) -> str:
    return f"> user: {content}"


def format_assistant(content: str) -> str:
    return f"assistant: {content}"


def format_tool(name: str, ok: bool, truncated: bool, output: str) -> str:
    status = "ok" if ok else "failed"
    if truncated:
        status += " (truncated)"
    first = output.splitlines()[0] if output.strip() else ""
    return f"[tool] {name} {status}" + (f" | {first}" if first else "")


def run_tui(session, store, workspace=None, *, model: str = "") -> None:
    console = Console()
    conversation: list[str] = []

    def status_text(state: str) -> str:
        parts = []
        if workspace is not None:
            parts.append(workspace.display())
        parts.append(f"model: {model}")
        parts.append(f"session: {session.session_id or 'new'}")
        parts.append(state)
        return " | ".join(parts)

    def build_layout(state: str, prompt: str) -> Layout:
        body = "\n".join(conversation) if conversation else "(empty)"
        layout = Layout()
        layout.split_column(
            Layout(Text(status_text(state), style="bold cyan"), name="status", size=1),
            Layout(Panel(body, title="conversation"), name="conv"),
            Layout(Text(prompt), name="input", size=1),
        )
        return layout

    def append_delta(idx: int, delta: str, live: Live) -> None:
        conversation[idx] += delta
        live.refresh()

    with Live(console=console, screen=False, refresh_per_second=10) as live:
        def ask(prompt: str) -> str:
            live.stop()
            try:
                return console.input(prompt)
            except (EOFError, KeyboardInterrupt):
                return ""
            finally:
                live.start()

        session.ask = ask
        while True:
            live.update(build_layout("idle", "> "))
            live.refresh()
            live.stop()
            try:
                raw = console.input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                live.start()
            task = raw.strip()
            if not task:
                continue
            if task.lower() in {"exit", "quit"}:
                break
            if task.startswith("/"):
                keep, out = handle_command(task, session, store)
                conversation.extend(out)
                if not keep:
                    break
                continue
            conversation.append(format_user(task))
            idx = len(conversation)
            conversation.append("assistant: ")
            live.update(build_layout("running", "[running…]"))
            live.refresh()
            result = session.run_task(task, on_delta=lambda d: append_delta(idx, d, live))
            conversation[idx] = format_assistant(result.final_text) if result.final_text else conversation[idx]
            if not result.finished:
                conversation.append(f"[agent] stopped: {result.reason}")
            if session.session_id:
                conversation.append(f"[session {session.session_id}]")
