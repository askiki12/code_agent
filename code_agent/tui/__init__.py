"""Textual terminal UI for interactive mode (replaces the rich tui.py)."""
from __future__ import annotations


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


def _append_tool_line(conversation: list[str], name: str, res) -> None:
    conversation.append(format_tool(name, res.ok, res.truncated, res.output))


def _line_style(line: str) -> str:
    if line.startswith("> user:"):
        return "bold cyan"
    if line.startswith("[tool]"):
        return "dim red" if " failed" in line else "dim"
    if line.startswith("[agent] stopped"):
        return "yellow"
    if line.startswith("[session"):
        return "magenta"
    if line.startswith("assistant:"):
        return "default"
    return "dim"


def run_tui(session, store, workspace=None, *, model: str = "") -> None:
    # Task 5 委托 CodeAgentApp；此处暂不允许实际进入（避免误导）。
    raise NotImplementedError("run_tui wired in Task 5")
