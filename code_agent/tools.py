"""Tool definitions and local executors (all self-implemented)."""
from __future__ import annotations

import glob as globlib
import os
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 8000
DEFAULT_COMMAND_TIMEOUT = 120  # seconds
MAX_LISTING_ENTRIES = 200
MAX_SEARCH_RESULTS = 500
MAX_GREP_LINE_CHARS = 200


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
    _schema(
        "write_file",
        "Create or overwrite a file (must be inside the workdir).",
        {
            "path": {"type": "string", "description": "File path (absolute or relative to workdir)"},
            "content": {"type": "string", "description": "Full file content"},
        },
        ["path", "content"],
    ),
    _schema(
        "edit_file",
        "Replace an exact substring in a file (must match uniquely unless replace_all).",
        {
            "path": {"type": "string", "description": "File path"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        ["path", "old_string", "new_string"],
    ),
    _schema(
        "run_command",
        "Run a shell command in the workdir (timeout and output limits apply).",
        {
            "command": {"type": "string", "description": "Shell command"},
            "timeout": {"type": "number", "description": f"Timeout in seconds (default {DEFAULT_COMMAND_TIMEOUT})"},
        },
        ["command"],
    ),
    _schema(
        "glob",
        "Find files by glob pattern (supports ** recursion). Refuses protected paths.",
        {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "path": {"type": "string", "description": "Directory to search (defaults to workdir)"},
        },
        ["pattern"],
    ),
]

def _write_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to write protected path: {path}")
    if not _inside_workdir(path, workdir):
        return ToolResult(ok=False, output=f"refusing to write outside workdir: {path}")
    content = args.get("content", "")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError as e:
        return ToolResult(ok=False, output=f"write failed: {e}")
    return ToolResult(ok=True, output=f"wrote {len(content)} chars to {path}")


def _edit_file(args: dict, workdir: str) -> ToolResult:
    path = _resolve(args.get("path", ""), workdir)
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to edit protected path: {path}")
    if not _inside_workdir(path, workdir):
        return ToolResult(ok=False, output=f"refusing to edit outside workdir: {path}")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if old == "":
        return ToolResult(ok=False, output="old_string must not be empty")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return ToolResult(ok=False, output=f"read failed: {e}")
    count = content.count(old)
    if count == 0:
        return ToolResult(ok=False, output=f"old_string not found in {path}")
    if count > 1 and not args.get("replace_all"):
        return ToolResult(
            ok=False,
            output=f"old_string matches {count} times in {path}; provide more context or set replace_all=true",
        )
    content = content.replace(old, new)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError as e:
        return ToolResult(ok=False, output=f"write failed: {e}")
    return ToolResult(ok=True, output=f"applied {count} replacement(s) in {path}")


def _run_command(args: dict, workdir: str) -> ToolResult:
    command = args.get("command", "")
    if not command.strip():
        return ToolResult(ok=False, output="command must not be empty")
    timeout = float(args.get("timeout", DEFAULT_COMMAND_TIMEOUT))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, output=f"command timed out after {timeout}s")
    except OSError as e:
        return ToolResult(ok=False, output=f"failed to run command: {e}")
    out = proc.stdout
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    out, truncated = truncate(out)
    return ToolResult(
        ok=proc.returncode == 0,
        output=out,
        truncated=truncated,
        exit_code=proc.returncode,
    )


def _glob(args: dict, workdir: str) -> ToolResult:
    pattern_src = args.get("pattern", "")
    if not pattern_src:
        return ToolResult(ok=False, output="pattern is required")
    path = _resolve(args.get("path", "."), workdir)
    if not os.path.isdir(path):
        return ToolResult(ok=False, output=f"directory not found: {path}")
    matches: list[str] = []
    for m in globlib.glob(os.path.join(path, pattern_src), recursive=True):
        if not os.path.isfile(m):
            continue
        rel = os.path.relpath(m, workdir)
        if _is_protected_path(rel):
            continue
        matches.append(rel)
    matches.sort()
    truncated = False
    if len(matches) > MAX_SEARCH_RESULTS:
        matches = matches[:MAX_SEARCH_RESULTS]
        truncated = True
    if not matches:
        return ToolResult(ok=True, output="(no matches)")
    out = "\n".join(matches)
    if truncated:
        out += f"\n...[search results truncated: more than {MAX_SEARCH_RESULTS} matched]"
    out, out_truncated = truncate(out)
    return ToolResult(ok=True, output=out, truncated=truncated or out_truncated)


_HANDLERS = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_dir": _list_dir,
    "run_command": _run_command,
    "glob": _glob,
}


def execute(name: str, args: dict, workdir: str) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, output=f"unknown tool: {name}")
    try:
        return handler(args, workdir)
    except Exception as e:  # noqa: BLE001 - last-resort guard
        return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")
