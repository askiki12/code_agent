"""Tool definitions and local executors (all self-implemented)."""
from __future__ import annotations

import fnmatch
import glob as globlib
import os
import re
import subprocess
from dataclasses import dataclass

from code_agent import web

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
        if part == ".code_agent":
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
    _schema(
        "grep",
        "Search file contents with a regex. Skips .git, protected and gitignored paths.",
        {
            "pattern": {"type": "string", "description": "Regex to search"},
            "path": {"type": "string", "description": "File or directory (defaults to workdir)"},
            "include": {"type": "string", "description": "fnmatch on filename, e.g. '*.py'"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Default 'content'",
            },
        },
        ["pattern"],
    ),
    _schema(
        "web_fetch",
        "Fetch a public web page (http/https) and return its title, readable text and first 10 links. Refuses non-public addresses (internal/private networks, file://).",
        {
            "url": {"type": "string", "description": "Public http(s) URL to fetch"},
        },
        ["url"],
    ),
    _schema(
        "web_search",
        "Search the web (DuckDuckGo Lite, keyless). Returns numbered results with title, real URL and snippet. Use web_fetch on a result URL for full content.",
        {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results (default 8)"},
        },
        ["query"],
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


def _has_symlink_component(match: str, base: str) -> bool:
    try:
        rel = os.path.relpath(match, base)
    except ValueError:
        return True
    cur = base
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def _glob(args: dict, workdir: str) -> ToolResult:
    pattern_src = args.get("pattern", "")
    if not pattern_src:
        return ToolResult(ok=False, output="pattern is required")
    path = _resolve(args.get("path", "."), workdir)
    if not os.path.isdir(path):
        return ToolResult(ok=False, output=f"directory not found: {path}")
    matches: list[str] = []
    truncated = False
    for m in globlib.iglob(os.path.join(path, pattern_src), recursive=True):
        if not os.path.isfile(m):
            continue
        rel = os.path.relpath(m, workdir)
        if _is_protected_path(rel):
            continue
        if _has_symlink_component(m, path):
            continue
        matches.append(rel)
        if len(matches) >= MAX_SEARCH_RESULTS:
            truncated = True
            break
    matches.sort()
    if not matches:
        return ToolResult(ok=True, output="(no matches)")
    out = "\n".join(matches)
    if truncated:
        out += f"\n...[search results truncated: more than {MAX_SEARCH_RESULTS} matched]"
    out, out_truncated = truncate(out)
    return ToolResult(ok=True, output=out, truncated=truncated or out_truncated)


def _clip_line(line: str) -> str:
    if len(line) <= MAX_GREP_LINE_CHARS:
        return line
    return line[:MAX_GREP_LINE_CHARS] + "..."


def _parse_gitignore_lines(text: str, base_dir: str) -> list[dict]:
    rules: list[dict] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negation = False
        if line.startswith("!"):
            negation = True
            line = line[1:].lstrip()
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        if not line:
            continue
        rules.append({
            "pattern": line,
            "negation": negation,
            "dir_only": dir_only,
            "anchored": anchored,
            "base_dir": base_dir,
        })
    return rules


def _load_gitignore_rules(dir_path: str) -> list[dict]:
    gi = os.path.join(dir_path, ".gitignore")
    if not os.path.isfile(gi):
        return []
    try:
        with open(gi, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return _parse_gitignore_lines(text, dir_path)


def _gitignore_ignored(rules: list[dict], abs_path: str, is_dir: bool) -> bool:
    ignored = False
    for r in rules:
        if r["dir_only"] and not is_dir:
            continue
        try:
            rel = os.path.relpath(abs_path, r["base_dir"])
        except ValueError:
            continue
        if r["anchored"] or "/" in r["pattern"]:
            match = fnmatch.fnmatch(rel, r["pattern"])
        else:
            match = fnmatch.fnmatch(rel, r["pattern"]) or fnmatch.fnmatch(
                os.path.basename(rel), r["pattern"]
            )
        if match:
            ignored = not r["negation"]
    return ignored


def _initial_gitignore_stack(root: str, workdir: str) -> list[dict]:
    rules: list[dict] = []
    if _inside_workdir(root, workdir):
        rel = os.path.relpath(root, workdir)
        dirs = [workdir]
        if rel != ".":
            cur = workdir
            for part in rel.split(os.sep):
                cur = os.path.join(cur, part)
                dirs.append(cur)
        for d in dirs:
            rules.extend(_load_gitignore_rules(d))
    else:
        rules.extend(_load_gitignore_rules(root))
    return rules


def _walk_searchable(root: str, workdir: str, rules: list[dict], include: str | None):
    """Yield (abs_path, rel_path) of searchable files under root, honoring gitignore."""
    if not os.path.isdir(root):
        return
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        abs_path = entry.path
        rel = os.path.relpath(abs_path, workdir)
        if _is_protected_path(rel):
            continue
        if entry.is_dir(follow_symlinks=False):
            if _gitignore_ignored(rules, abs_path, True):
                continue
            yield from _walk_searchable(abs_path, workdir, rules + _load_gitignore_rules(abs_path), include)
        elif entry.is_file(follow_symlinks=False):
            if _gitignore_ignored(rules, abs_path, False):
                continue
            if include and not fnmatch.fnmatch(entry.name, include):
                continue
            yield abs_path, rel


def _grep(args: dict, workdir: str) -> ToolResult:
    pattern_src = args.get("pattern", "")
    if not pattern_src:
        return ToolResult(ok=False, output="pattern is required")
    output_mode = args.get("output_mode", "content")
    if output_mode not in {"content", "files_with_matches", "count"}:
        return ToolResult(ok=False, output=f"invalid output_mode: {output_mode}")
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        pattern = re.compile(pattern_src, flags)
    except re.error as e:
        return ToolResult(ok=False, output=f"invalid regex: {e}")
    path = _resolve(args.get("path", "."), workdir)
    if not os.path.exists(path):
        return ToolResult(ok=False, output=f"path not found: {path}")
    if _is_protected_path(path):
        return ToolResult(ok=False, output=f"refusing to search protected path: {path}")
    include = args.get("include")
    is_dir = os.path.isdir(path)

    hits: list[tuple[str, int, str]] = []
    file_set: set[str] = set()
    counts: dict[str, int] = {}
    total = 0
    truncated = False

    def process(abs_path: str, rel: str) -> None:
        nonlocal total, truncated
        try:
            with open(abs_path, "rb") as f:
                head = f.read(8192)
        except OSError:
            return
        if b"\x00" in head:
            return
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        file_matches: list[tuple[int, str]] = [
            (lineno, line.rstrip("\r\n"))
            for lineno, line in enumerate(lines, 1)
            if pattern.search(line.rstrip("\r\n"))
        ]
        if not file_matches:
            return
        if output_mode == "count":
            counts[rel] = len(file_matches)
            total += 1
            if total >= MAX_SEARCH_RESULTS:
                truncated = True
            return
        file_set.add(rel)
        if output_mode == "files_with_matches":
            total += 1
            if total >= MAX_SEARCH_RESULTS:
                truncated = True
            return
        for lineno, line in file_matches:
            if total >= MAX_SEARCH_RESULTS:
                truncated = True
                break
            hits.append((rel, lineno, line))
            total += 1

    if is_dir:
        rules = _initial_gitignore_stack(path, workdir)
        for abs_path, rel in _walk_searchable(path, workdir, rules, include):
            process(abs_path, rel)
            if total >= MAX_SEARCH_RESULTS:
                truncated = True
                break
    else:
        base = os.path.dirname(path)
        rules = _initial_gitignore_stack(base, workdir)
        if _gitignore_ignored(rules, path, False):
            return ToolResult(ok=True, output="(no matches)")
        if include and not fnmatch.fnmatch(os.path.basename(path), include):
            return ToolResult(ok=True, output="(no matches)")
        process(path, os.path.relpath(path, workdir))

    if output_mode == "count":
        lines_out = [f"{rel}:{n}" for rel, n in sorted(counts.items())]
    elif output_mode == "files_with_matches":
        lines_out = sorted(file_set)
    else:
        lines_out = [
            f"{rel}:{lineno}:{_clip_line(line)}"
            for rel, lineno, line in sorted(hits)
        ]
    if not lines_out:
        return ToolResult(ok=True, output="(no matches)")
    out = "\n".join(lines_out)
    if truncated:
        out += f"\n...[search truncated: hit {MAX_SEARCH_RESULTS} result limit]"
    out, out_truncated = truncate(out)
    return ToolResult(ok=True, output=out, truncated=truncated or out_truncated)


def _web_fetch(args: dict, workdir: str) -> ToolResult:
    url = args.get("url", "")
    if not url:
        return ToolResult(ok=False, output="url is required")
    try:
        content = web.fetch(url)
    except web.WebFetchError as e:
        return ToolResult(ok=False, output=f"web_fetch failed: {e}")
    parts: list[str] = []
    if content.title:
        parts.append(f"Title: {content.title}")
    if content.text:
        parts.append(content.text)
    if content.links:
        parts.append("Links:")
        parts.extend(f"- {link}" for link in content.links)
    if not parts:
        parts.append("(empty page)")
    out, truncated = truncate("\n\n".join(parts))
    return ToolResult(ok=True, output=out, truncated=truncated)


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


def execute(name: str, args: dict, workdir: str) -> ToolResult:
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, output=f"unknown tool: {name}")
    try:
        return handler(args, workdir)
    except Exception as e:  # noqa: BLE001 - last-resort guard
        return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")
