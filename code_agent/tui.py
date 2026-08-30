"""Rich terminal UI for interactive mode."""
from __future__ import annotations

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
