"""Command-line entry point."""
from __future__ import annotations

import argparse
import os
import sys

from code_agent.agent import AgentSession
from code_agent.llm import LLMClient, resolve_context_window
from code_agent.permissions import Policy
from code_agent.session import SessionStore
from code_agent.skills import SkillRegistry
from code_agent.workspace import Workspace

_DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
}


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from an optional .env file (stdlib, no overrides)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent", description="A self-built coding agent"
    )
    parser.add_argument("--prompt", help="Run a one-shot task and exit")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive session")
    parser.add_argument("--list-sessions", action="store_true", help="List saved sessions and exit")
    parser.add_argument("--resume", help="Resume a session by id")
    parser.add_argument("--allow", action="append", default=[], metavar="TOOL:PATTERN", help="Allow rule (repeatable)")
    parser.add_argument("--deny", action="append", default=[], metavar="TOOL:PATTERN", help="Deny rule (repeatable)")
    parser.add_argument("--ask", action="append", default=[], metavar="TOOL:PATTERN", help="Ask rule (repeatable)")
    parser.add_argument("--workdir", default=".", help="Working directory (default: current dir)")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="API key (default: env CODE_AGENT_API_KEY)")
    parser.add_argument("--model", help="Model name (default: env CODE_AGENT_MODEL)")
    parser.add_argument("--max-iterations", type=int, default=20, help="Max agent iterations")
    parser.add_argument("--max-context-tokens", type=int, default=90000, help="Context budget in tokens")
    parser.add_argument("--context-window", type=int, default=None,
                        help="Model context window in tokens (default: auto-detect)")
    parser.add_argument("--debug", action="store_true", help="Print debug logs")
    return parser


def _make_client(args: argparse.Namespace) -> LLMClient:
    base_url = args.base_url or os.environ.get("CODE_AGENT_BASE_URL") or _DEFAULTS["base_url"]
    api_key = args.api_key or os.environ.get("CODE_AGENT_API_KEY")
    if not api_key:
        raise SystemExit("error: CODE_AGENT_API_KEY is not set (or pass --api-key)")
    model = args.model or os.environ.get("CODE_AGENT_MODEL") or _DEFAULTS["model"]
    return LLMClient(base_url=base_url, api_key=api_key, model=model, debug=args.debug)


def _run(session: AgentSession, task: str) -> None:
    result = session.run_task(task, on_delta=lambda d: print(d, end="", flush=True))
    if result.final_text:
        print()
    if not result.finished:
        print(f"\n[agent] stopped: {result.reason}", file=sys.stderr)


def _make_store(workdir: str) -> SessionStore:
    return SessionStore(os.path.join(workdir, ".code_agent", "sessions"))


def handle_command(command: str, session, store: SessionStore) -> tuple[bool, list[str]]:
    parts = command.split(maxsplit=1)
    cmd = parts[0]
    out: list[str] = []
    if cmd == "/new":
        session.new_session()
        out.append("New session started.")
    elif cmd == "/list":
        for s in store.list_sessions():
            out.append(f"{s['id']}  {s['title'] or ''}  ({s['message_count']} msgs)")
    elif cmd == "/resume":
        sid = parts[1] if len(parts) > 1 else ""
        if not sid:
            out.append("usage: /resume <session-id>")
        else:
            try:
                session.load_session(sid)
                out.append(f"Resumed session {sid}.")
            except KeyError:
                out.append(f"session not found: {sid}")
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
    elif cmd == "/exit":
        return False, []
    else:
        out.append(f"unknown command: {cmd}")
    return True, out


def _handle_command(command: str, session, store: SessionStore) -> bool:
    keep, out = handle_command(command, session, store)
    for line in out:
        print(line)
    return keep


def _use_tui() -> bool:
    return bool(sys.stdout.isatty()) and os.environ.get("NO_TUI") is None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.prompt and not args.interactive and not args.list_sessions:
        parser.print_help()
        return 1
    _load_dotenv()
    workdir = os.path.abspath(args.workdir)
    try:
        workspace = Workspace(workdir)
    except OSError:
        workspace = None
    store = _make_store(workdir)
    if args.list_sessions:
        if args.prompt or args.interactive:
            print("note: --list-sessions overrides --prompt/--interactive", file=sys.stderr)
        for s in store.list_sessions():
            print(f"{s['id']}  {s['title'] or ''}  ({s['message_count']} msgs, {s['updated_at']})")
        return 0
    llm = _make_client(args)
    if args.context_window:
        window = args.context_window
    else:
        window = resolve_context_window(llm.model, llm.base_url, llm.api_key)
    policy = Policy(allow=args.allow, deny=args.deny, ask=args.ask)
    skills = SkillRegistry(workdir)
    try:
        session = AgentSession(
            workdir=workdir,
            llm=llm,
            max_iterations=args.max_iterations,
            max_context_tokens=args.max_context_tokens,
            context_window=window,
            debug=args.debug,
            store=store,
            session_id=args.resume,
            resume=args.resume is not None,
            workspace=workspace,
            policy=policy,
            interact=args.interactive,
            skills=skills if skills.scan() else None,
        )
    except KeyError:
        print(f"session not found: {args.resume}", file=sys.stderr)
        return 1
    if args.prompt:
        _run(session, args.prompt)
        return 0
    if _use_tui():
        from code_agent.tui import run_tui
        run_tui(session, store, workspace, model=llm.model)
        return 0
    if workspace is not None:
        sessions = store.list_sessions()
        line = workspace.display() + f" | sessions: {len(sessions)}"
        last = workspace.last_session_id
        if last and any(s["id"] == last for s in sessions):
            line += f" | last: {last}"
            print(f"Tip: /resume {last} 续接上次会话")
        print(line)
    print("Interactive mode. Type 'exit', 'quit' or '/exit' to leave. Commands: /new /list /resume <id>")
    while True:
        try:
            task = input("> ")
        except EOFError:
            break
        task = task.strip()
        if not task:
            continue
        if task.lower() in {"exit", "quit"}:
            break
        if task.startswith("/"):
            if not _handle_command(task, session, store):
                break
            continue
        _run(session, task)
        if session.session_id:
            print(f"[session {session.session_id}]")
    return 0
