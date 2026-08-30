"""Agent session loop, termination, and error recovery."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable

from code_agent.context import Conversation
from code_agent.llm import LLMError
from code_agent.permissions import Policy
from code_agent.session import SessionStore, _make_title
from code_agent.skills import SkillRegistry
from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute, truncate
from code_agent.workspace import Workspace

SYSTEM_PROMPT = """You are a coding agent. You work inside a local workspace and complete software tasks autonomously.

Available tools:
- read_file: read text files
- write_file: create or overwrite files
- edit_file: replace an exact substring in a file (must match uniquely)
- list_dir: list directory contents
- run_command: run a shell command (has a timeout)
- glob: find files by glob pattern (e.g. **/*.py)
- grep: search file contents with a regex
- web_fetch: fetch a public web page's title/text/links (refuses internal/private addresses)
- web_search: search the web (DuckDuckGo Lite) returning titles/URLs/snippets

Rules:
1. Plan each step. Prefer small, verifiable changes.
2. Use run_command to verify your work (e.g. run tests).
3. When a tool fails, read the error, adjust, and retry. Do not repeat the exact same failing call.
4. Do NOT read or write protected paths such as .env, .env.* or .git.
5. When the task is complete, reply with a short final summary and stop making tool calls.
6. When external facts are uncertain, use web_search to discover candidate URLs, then use web_fetch to read the full page — never guess from memory.
"""

MAX_ITERATIONS_DEFAULT = 20
MAX_CONSECUTIVE_FAILURES = 3

SUBAGENT_MAX_ITERATIONS = 10

SUBAGENT_PROMPT_EXTRA = (
    "\n\nYou are a subagent delegated a sub-task. Complete it independently "
    "using the available tools, then reply with a concise report of what you "
    "did and found. You cannot delegate to sub-subagents."
)

_DISPATCH_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": "Delegate a sub-task to a subagent that runs its own agent loop with all tools except subagent dispatch. Returns the subagent's final report.",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Sub-task description"}},
            "required": ["task"],
        },
    },
}

_USE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": "Load a skill's instructions into context. Returns the skill content; follow it.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name"}},
            "required": ["name"],
        },
    },
}


@dataclass
class RunResult:
    final_text: str
    iterations: int
    finished: bool
    reason: str


class AgentSession:
    def __init__(
        self,
        *,
        workdir: str,
        llm: Any,
        max_iterations: int = MAX_ITERATIONS_DEFAULT,
        max_context_tokens: int = 90000,
        debug: bool = False,
        store: SessionStore | None = None,
        session_id: str | None = None,
        resume: bool = False,
        workspace: Workspace | None = None,
        policy: Policy | None = None,
        interact: bool = False,
        ask: Callable[[str], str] | None = None,
        skills: SkillRegistry | None = None,
        allow_subagent: bool = True,
    ) -> None:
        self.workdir = workdir
        self.llm = llm
        self.max_iterations = max_iterations
        self.max_context_tokens = max_context_tokens
        self.debug = debug
        self.store = store
        self.workspace = workspace
        self.policy = policy
        self.interact = interact
        self.ask = ask
        self.session_id = session_id
        self.skills = skills
        self.allow_subagent = allow_subagent
        self._on_delta = None
        skills_section = ""
        if skills is not None:
            entries = "\n".join(f"- {s.name}: {s.description}" for s in skills.scan())
            if entries:
                skills_section = (
                    "\n\nAvailable skills:\n" + entries
                    + "\nUse the use_skill tool to load a skill when the task matches."
                )
        self._system_prompt = (
            SYSTEM_PROMPT
            + skills_section
            + (SUBAGENT_PROMPT_EXTRA if not allow_subagent else "")
        )
        if resume:
            if session_id is None:
                raise ValueError("resume=True requires session_id")
            self.load_session(session_id)
        else:
            self.conversation = Conversation()
            self.conversation.add_system(self._system_prompt)

    def run_task(
        self,
        task: str,
        on_delta: Callable[[str], None] | None = None,
        on_tool: Callable[[str, ToolResult], None] | None = None,
        on_assistant_start: Callable[[], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
    ) -> RunResult:
        self.conversation.add_user(task)
        self._on_delta = on_delta
        consecutive_failures = 0
        llm_error_count = 0
        try:
            for iteration in range(1, self.max_iterations + 1):
                if self.debug:
                    print(f"[agent] iteration {iteration}")
                messages = self.conversation.build_messages(self.max_context_tokens)
                try:
                    tools = TOOL_SCHEMAS + ([_USE_SKILL_SCHEMA] if self.skills is not None else [])
                    if self.allow_subagent:
                        tools = tools + [_DISPATCH_SUBAGENT_SCHEMA]
                    if on_assistant_start is not None:
                        on_assistant_start()
                    response = self.llm.chat(messages, tools=tools, on_delta=on_delta)
                except LLMError as e:
                    llm_error_count += 1
                    if llm_error_count >= MAX_CONSECUTIVE_FAILURES:
                        return RunResult(
                            final_text="",
                            iterations=iteration,
                            finished=False,
                            reason=f"llm error: {e}",
                        )
                    self.conversation.add_user(
                        f"[system] An LLM error occurred: {e}. "
                        "Please reply in plain text without tool calls, or continue if possible."
                    )
                    continue
                llm_error_count = 0
                self.conversation.add_assistant(response.content, response.tool_calls or None)
                if not response.tool_calls:
                    return RunResult(
                        final_text=response.content,
                        iterations=iteration,
                        finished=True,
                        reason="complete",
                    )
                round_failed = False
                for tc in response.tool_calls:
                    if on_tool_start is not None:
                        on_tool_start(tc.name, tc.arguments)
                    result = self._run_tool(tc)
                    if on_tool is not None:
                        on_tool(tc.name, result)
                    if not result.ok:
                        round_failed = True
                    if self.debug:
                        print(f"[tool] {tc.name}: ok={result.ok} truncated={result.truncated}")
                    self.conversation.add_tool(tc.id, tc.name, result.as_message())
                consecutive_failures = consecutive_failures + 1 if round_failed else 0
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    return RunResult(
                        final_text="",
                        iterations=iteration,
                        finished=False,
                        reason="too many consecutive tool failures",
                    )
            return RunResult(
                final_text="",
                iterations=self.max_iterations,
                finished=False,
                reason="max_iterations",
            )
        finally:
            if self.store is not None:
                title = self._title()
                try:
                    if self.session_id is None:
                        self.session_id = self.store.create(title)
                    self.store.save(self.session_id, self.conversation.messages, title=title)
                    if self.workspace is not None:
                        self.workspace.touch_session(self.session_id)
                except (OSError, ValueError) as e:
                    print(f"[agent] warning: failed to persist session/workspace: {e}", file=sys.stderr)

    def _title(self) -> str:
        for m in self.conversation.messages:
            if m.get("role") == "user":
                return _make_title(str(m.get("content", "")))
        return ""

    def new_session(self) -> None:
        self.conversation = Conversation()
        self.conversation.add_system(self._system_prompt)
        self.session_id = None

    def load_session(self, session_id: str) -> None:
        if self.store is None:
            raise ValueError("no session store configured")
        _, messages = self.store.load(session_id)
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages)
        self.conversation = Conversation.from_jsonl(text, system_prompt=self._system_prompt)
        self.session_id = session_id

    def _run_tool(self, tc) -> ToolResult:
        if tc.name == "dispatch_subagent":
            if not self.allow_subagent:
                return ToolResult(ok=False, output="subagent dispatch is disabled for subagents")
            return self._dispatch_subagent(tc.arguments, on_delta=self._on_delta)
        if tc.name == "use_skill" and self.skills is not None:
            return self._use_skill(tc.arguments)
        if self.policy is not None:
            result = self.policy.check(tc.name, tc.arguments, interact=self.interact, ask=self.ask)
            if result.decision == "deny":
                reason = result.reason or tc.name
                return ToolResult(ok=False, output=f"permission denied: {reason}")
        try:
            return execute(tc.name, tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")

    def _use_skill(self, arguments: dict) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult(ok=False, output="skill arguments must be an object")
        name = arguments.get("name", "")
        if not name:
            return ToolResult(ok=False, output="skill name is required")
        content = self.skills.load(name)
        if content is None:
            return ToolResult(ok=False, output=f"skill not found: {name}")
        return ToolResult(ok=True, output=content)

    def _dispatch_subagent(self, arguments, on_delta=None) -> ToolResult:
        if not isinstance(arguments, dict) or not str(arguments.get("task", "")).strip():
            return ToolResult(ok=False, output="task is required")
        task = str(arguments["task"]).strip()
        try:
            sub = AgentSession(
                workdir=self.workdir,
                llm=self.llm,
                max_iterations=SUBAGENT_MAX_ITERATIONS,
                max_context_tokens=self.max_context_tokens,
                debug=self.debug,
                policy=self.policy,
                interact=self.interact,
                ask=self.ask,
                skills=self.skills,
                allow_subagent=False,
            )
            sub_result = sub.run_task(task, on_delta=on_delta)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"subagent dispatch failed: {type(e).__name__}: {e}")
        if sub_result.final_text:
            out = sub_result.final_text
        else:
            out = f"(subagent returned no report; status: {sub_result.reason})"
        out, truncated = truncate(out)
        return ToolResult(ok=True, output=out, truncated=truncated)
