"""Tool definitions and local executors (all self-implemented)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 8000
DEFAULT_COMMAND_TIMEOUT = 120  # seconds
MAX_LISTING_ENTRIES = 200


@dataclass
class ToolResult:
    ok: bool
    output: str
    truncated: bool = False
    exit_code: int | None = None

    def as_message(self) -> str:
        return self.output


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    marker = f"\n...[TRUNCATED: {len(text) - limit} chars omitted]...\n"
    return head + marker + tail, True


def _is_protected_path(path: str) -> bool:
    parts = [p for p in path.replace(os.sep, "/").split("/") if p]
    for part in parts:
        if part == ".git":
            return True
        if part.startswith(".env") and part != ".env.example":
            return True
    return False


def _inside_workdir(path: str, workdir: str) -> bool:
    wd = os.path.realpath(workdir)
    p = os.path.realpath(path)
    try:
        return os.path.commonpath([wd, p]) == wd
    except ValueError:
        return False


def _resolve(path: str, workdir: str) -> str:
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(workdir, path))


def _read_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to read protected path: {path}")
    if not os.path.isfile(path):
        return ToolResult(ok=False, output=f"file not found: {path}")
    offset = args.get("offset")
    limit = args.get("limit")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return ToolResult(ok=False, output=f"read failed: {e}")
    start = offset - 1 if isinstance(offset, int) and offset >= 1 else 0
    end = start + limit if isinstance(limit, int) and limit >= 1 else len(lines)
    selected = lines[start:end]
    if start == 0 and end >= len(lines):
        body = "".join(selected)
    else:
        body = "".join(f"{start + i + 1:>6}  {line}" for i, line in enumerate(selected))
    body, truncated = truncate(body)
    return ToolResult(ok=True, output=body, truncated=truncated)


def _list_dir(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", "."), workdir)
    if not os.path.isdir(path):
        return ToolResult(ok=False, output=f"directory not found: {path}")
    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return ToolResult(ok=False, output=f"list failed: {e}")
    lines: list[str] = []
    shown = 0
    for e in entries:
        if e.name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        if e.is_dir():
            lines.append(f"{e.name}/")
        else:
            try:
                size = os.path.getsize(e.path)
            except OSError:
                size = 0
            lines.append(f"{e.name}  ({size} bytes)")
        shown += 1
        if shown >= MAX_LISTING_ENTRIES:
            lines.append(f"...[{shown} entries shown, more omitted]")
            break
    if not lines:
        lines.append("(empty directory)")
    return ToolResult(ok=True, output="\n".join(lines))


def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _schema(
        "read_file",
        "Read a text file (with optional line range). Refuses protected paths.",
        {
            "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
            "offset": {"type": "integer", "description": "1-based start line"},
            "limit": {"type": "integer", "description": "Max lines to read"},
        },
        ["path"],
    ),
    _schema(
        "list_dir",
        "List directory entries with type and size. Skips .git and caches.",
        {"path": {"type": "string", "description": "Directory path (defaults to workdir)"}},
        [],
    ),
]

_HANDLERS = {
    "read_file": _read_file,
    "list_dir": _list_dir,
}


def execute(name: str, args: dict, workdir: str) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, output=f"unknown tool: {name}")
    try:
        return handler(args, workdir)
    except Exception as e:  # noqa: BLE001 - last-resort guard
        return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")
