"""Command-line entry point."""
from __future__ import annotations

import argparse
import os
import sys

from code_agent.agent import AgentSession
from code_agent.llm import LLMClient

_DEFAULTS = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent", description="A self-built coding agent"
    )
    parser.add_argument("--prompt", help="Run a one-shot task and exit")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive session")
    parser.add_argument("--workdir", default=".", help="Working directory (default: current dir)")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="API key (default: env CODE_AGENT_API_KEY)")
    parser.add_argument("--model", help="Model name (default: env CODE_AGENT_MODEL)")
    parser.add_argument("--max-iterations", type=int, default=20, help="Max agent iterations")
    parser.add_argument("--max-context-tokens", type=int, default=90000, help="Context budget in tokens")
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.prompt and not args.interactive:
        parser.print_help()
        return 1
    workdir = os.path.abspath(args.workdir)
    llm = _make_client(args)
    session = AgentSession(
        workdir=workdir,
        llm=llm,
        max_iterations=args.max_iterations,
        max_context_tokens=args.max_context_tokens,
        debug=args.debug,
    )
    if args.prompt:
        _run(session, args.prompt)
        return 0
    print("Interactive mode. Type 'exit' or 'quit' to leave.")
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
        _run(session, task)
    return 0
