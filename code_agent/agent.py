"""Agent session loop, termination, and error recovery."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

from code_agent.context import Conversation, estimate_tokens
from code_agent.llm import LLMError, Usage
from code_agent.memory import MemoryStore
from code_agent.permissions import Policy
from code_agent.session import SessionStore, _make_title
from code_agent.skills import SkillRegistry
from code_agent.tools import BASE_TOOLS, Tool, ToolRegistry, ToolResult, truncate
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

Choosing tools:
- Discover before reading: use list_dir/glob to locate files and grep to search contents — do not read whole files blindly.
- Read large files with read_file using offset/limit.
- Prefer edit_file (exact, unique substring) for surgical changes; use write_file for new files or whole-file rewrites.
- Verify your work with run_command (tests, compile, etc.).
- For external facts, use web_search to discover URLs, then web_fetch to read the full page.
"""

MAX_ITERATIONS_DEFAULT = 20
MAX_CONSECUTIVE_FAILURES = 3

SUBAGENT_MAX_ITERATIONS = 10

SUBAGENT_PROMPT_EXTRA = (
    "\n\nYou are a subagent delegated a sub-task. Complete it independently "
    "using the available tools, then reply with a concise report of what you "
    "did and found. You cannot delegate to sub-subagents."
)

DISPATCH_GUIDANCE = (
    "\n\nWhen to delegate: dispatch_subagent(task) runs an independent subagent "
    "that keeps all tools except subagent dispatch and returns a concise report. "
    "Use it for a sub-task that does not need shared context; do not use it for trivial work."
)

MEMORY_GUIDANCE = (
    "\n\nProject memory: recall(query, top_k) searches durable knowledge saved from "
    "prior sessions — query it before re-reading long documents. "
    "remember(content, tags) saves new facts/decisions/gotchas for future sessions. "
    "Relevant memories are auto-injected at task start and auto-summarized on success."
)

SKILL_GUIDANCE_USE = (
    "\n\nSkills: use_skill(name) loads a skill's SKILL.md instructions into context — "
    "call it when the task matches an available skill, then follow its steps."
)

SKILL_GUIDANCE_CREATE = (
    "\n\nAuthoring skills: a skill is a SKILL.md file at "
    "<workdir>/.code_agent/skills/<name>/SKILL.md (project-level) or "
    "~/.code_agent/skills/<name>/SKILL.md (user-level; project wins on name conflict). "
    "Format: frontmatter then a markdown body:\n"
    "---\nname: <letters, digits, - or _>\ndescription: one-line 'when to use this'\n---\n"
    "<reusable steps, gotchas, exact commands>\n"
    "Create it with create_skill(name, description, content) — it writes the file for you; "
    ".code_agent is a protected path, so never write_file/edit_file a skill by hand. "
    "The description shows in the skill list and picker, so make it matchable. "
    "Write a skill after finishing a reusable, non-trivial workflow."
)

class UseSkillTool(Tool):
    name = "use_skill"
    description = "Load a skill's instructions into context. Returns the skill content; follow it."
    parameters = {"name": {"type": "string", "description": "Skill name"}}
    required = ["name"]
    bypass_policy = True

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._use_skill(args)


class DispatchSubagentTool(Tool):
    name = "dispatch_subagent"
    description = ("Delegate a sub-task to a subagent that runs its own agent loop with all tools "
                  "except subagent dispatch. Returns the subagent's final report.")
    parameters = {"task": {"type": "string", "description": "Sub-task description"}}
    required = ["task"]
    bypass_policy = True

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        if not self._session.allow_subagent:
            return ToolResult(ok=False, output="subagent dispatch is disabled for subagents")
        return self._session._dispatch_subagent(args, on_delta=self._session._on_delta)


class RememberTool(Tool):
    name = "remember"
    description = "Save a durable piece of project knowledge to the project memory for future sessions."
    parameters = {
        "content": {"type": "string", "description": "The knowledge/fact/gotcha to remember"},
        "tags": {"type": "string", "description": "Comma-separated tags (optional)"},
    }
    required = ["content"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._remember(args)


class RecallTool(Tool):
    name = "recall"
    description = "Search the project memory for relevant knowledge from prior sessions."
    parameters = {
        "query": {"type": "string", "description": "What to search for"},
        "top_k": {"type": "integer", "description": "Max results (default 3)"},
    }
    required = ["query"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._recall(args)


class CreateSkillTool(Tool):
    name = "create_skill"
    description = "Save a reusable workflow as a project skill (SKILL.md). It becomes available via use_skill in future sessions."
    parameters = {
        "name": {"type": "string", "description": "Skill name (letters, digits, - and _)"},
        "description": {"type": "string", "description": "Short description"},
        "content": {"type": "string", "description": "Markdown instructions body"},
    }
    required = ["name", "description", "content"]

    def __init__(self, session, *, visible: bool) -> None:
        super().__init__()
        self._session = session
        self.visible = visible

    def execute(self, args: dict, workdir: str) -> ToolResult:
        return self._session._create_skill(args)


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
        context_window: int | None = None,
        memory: bool = False,
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
        self.memory = memory
        self._memory = MemoryStore(os.path.join(workdir, ".code_agent", "memory")) if memory else None
        self._memory_injected = False
        self.last_usage: Usage | None = None
        self.context_window = context_window if context_window else 1_000_000
        if context_window:
            self.max_context_tokens = min(max_context_tokens, int(0.7 * context_window))
        self._on_delta = None
        tools = list(BASE_TOOLS)
        tools.append(UseSkillTool(self, visible=self.skills is not None))
        tools.append(DispatchSubagentTool(self, visible=self.allow_subagent))
        if memory:
            tools.append(RememberTool(self, visible=True))
            tools.append(RecallTool(self, visible=True))
            tools.append(CreateSkillTool(self, visible=True))
        self._registry = ToolRegistry(tools)
        skills_section = ""
        if skills is not None:
            entries = "\n".join(f"- {s.name}: {s.description}" for s in skills.scan())
            if entries:
                skills_section = (
                    "\n\nAvailable skills:\n" + entries
                    + SKILL_GUIDANCE_USE
                    + (SKILL_GUIDANCE_CREATE if memory else "")
                )
        self._system_prompt = (
            SYSTEM_PROMPT
            + (DISPATCH_GUIDANCE if allow_subagent else "")
            + skills_section
            + (MEMORY_GUIDANCE if memory else "")
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
        on_stats: Callable[[Usage], None] | None = None,
    ) -> RunResult:
        self.conversation.add_user(task)
        try:
            self._inject_memory(task)
        except Exception:  # noqa: BLE001 - memory injection must never break the loop
            pass
        self._on_delta = on_delta
        result: RunResult | None = None
        try:
            result = self._run_loop(on_delta, on_tool, on_assistant_start, on_tool_start, on_stats)
        finally:
            self._persist()
        if result is not None and self._memory is not None and result.finished:
            self._auto_memorize()
        return result

    def _run_loop(
        self,
        on_delta: Callable[[str], None] | None,
        on_tool: Callable[[str, ToolResult], None] | None,
        on_assistant_start: Callable[[], None] | None,
        on_tool_start: Callable[[str, dict], None] | None,
        on_stats: Callable[[Usage], None] | None,
    ) -> RunResult:
        """原 run_task 的 for 循环体（含内层 LLMError 处理与全部 return），去掉外层 try/finally。"""
        consecutive_failures = 0
        llm_error_count = 0
        for iteration in range(1, self.max_iterations + 1):
            if self.debug:
                print(f"[agent] iteration {iteration}")
            messages = self.conversation.build_messages(self.max_context_tokens)
            try:
                tools = self._registry.schemas()
                if on_assistant_start is not None:
                    on_assistant_start()
                response = self.llm.chat(messages, tools=tools, on_delta=on_delta)
            except LLMError as e:
                llm_error_count += 1
                if llm_error_count >= MAX_CONSECUTIVE_FAILURES:
                    return RunResult(final_text="", iterations=iteration, finished=False, reason=f"llm error: {e}")
                self.conversation.add_user(
                    f"[system] An LLM error occurred: {e}. "
                    "Please reply in plain text without tool calls, or continue if possible."
                )
                continue
            llm_error_count = 0
            if response.usage is not None:
                usage = response.usage
            else:
                usage = Usage(
                    prompt_tokens=sum(
                        estimate_tokens(str(m.get("content", ""))) for m in messages
                    ),
                    heuristic=True,
                )
            self.last_usage = usage
            if on_stats is not None:
                on_stats(usage)
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

    def _persist(self) -> None:
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

    def _inject_memory(self, task: str) -> None:
        if self._memory is None or self._memory_injected:
            return
        self._memory_injected = True
        entries = self._memory.recall(task, top_k=3)
        if not entries:
            return
        lines = "\n".join(f"- {e.content}" for e in entries)
        self.conversation.add_system(
            "[Project memory]\nPrior sessions recorded about this project (use recall for more; remember to save new knowledge):\n"
            + lines
        )

    def _auto_memorize(self) -> None:
        try:
            resp = self.llm.chat(
                [
                    {"role": "system", "content": "You are an agent that just finished a task in a coding project."},
                    {
                        "role": "user",
                        "content": "Extract 1-3 durable, reusable pieces of project knowledge from this conversation "
                        "(facts, decisions, gotchas, key file locations). Reply with a JSON array of strings only.\n\n"
                        + self._memory_transcript(),
                    },
                ]
            )
            content = resp.content.strip()
            try:
                items = json.loads(content)
                entries = [str(i) for i in items if isinstance(i, str)] if isinstance(items, list) else []
            except json.JSONDecodeError:
                entries = [content]
            for e in entries:
                if e.strip():
                    self._memory.add(e.strip(), source_session=self.session_id or "")
        except Exception:  # noqa: BLE001 - auto-memorize must never break the loop
            pass

    def _memory_transcript(self) -> str:
        parts = []
        for m in self.conversation.messages[-40:]:
            role = m.get("role", "")
            text = str(m.get("content", ""))[:200]
            parts.append(f"{role}: {text}")
        return "\n".join(parts)[:6000]

    def _title(self) -> str:
        for m in self.conversation.messages:
            if m.get("role") == "user":
                return _make_title(str(m.get("content", "")))
        return ""

    def current_title(self) -> str:
        if self.session_id is None or self.store is None:
            return ""
        return self.store.get_title(self.session_id)

    def new_session(self) -> None:
        self.conversation = Conversation()
        self.conversation.add_system(self._system_prompt)
        self.session_id = None
        self.last_usage = None
        self._memory_injected = False

    def load_session(self, session_id: str) -> None:
        if self.store is None:
            raise ValueError("no session store configured")
        _, messages = self.store.load(session_id)
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages)
        self.conversation = Conversation.from_jsonl(text, system_prompt=self._system_prompt)
        self.session_id = session_id
        self._memory_injected = False
        self.last_usage = Usage(
            prompt_tokens=sum(
                estimate_tokens(str(m.get("content", ""))) for m in self.conversation.messages
            ),
            heuristic=True,
        )

    def _run_tool(self, tc) -> ToolResult:
        tool = self._registry.get(tc.name)
        if tool is None:
            return ToolResult(ok=False, output=f"unknown tool: {tc.name}")
        if not tool.bypass_policy and self.policy is not None:
            result = self.policy.check(tc.name, tc.arguments, interact=self.interact, ask=self.ask)
            if result.decision == "deny":
                return ToolResult(ok=False, output=f"permission denied: {result.reason or tc.name}")
        try:
            msg = tool.validate(tc.arguments)
            if msg:
                return ToolResult(ok=False, output=msg)
            return tool.execute(tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crashed: {type(e).__name__}: {e}")

    def rename_session(self, title: str) -> str:
        if self.store is None:
            raise ValueError("no session store configured")
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        if self.session_id is None:
            self.session_id = self.store.create(title)
        self.store.rename(self.session_id, title)
        return title

    def _use_skill(self, arguments: dict) -> ToolResult:
        if self.skills is None:
            return ToolResult(ok=False, output="skills are not available")
        if not isinstance(arguments, dict):
            return ToolResult(ok=False, output="skill arguments must be an object")
        name = arguments.get("name", "")
        if not name:
            return ToolResult(ok=False, output="skill name is required")
        content = self.skills.load(name)
        if content is None:
            return ToolResult(ok=False, output=f"skill not found: {name}")
        return ToolResult(ok=True, output=content)

    def _remember(self, arguments: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, output="memory is disabled")
        if not isinstance(arguments, dict):
            return ToolResult(ok=False, output="arguments must be an object")
        content = str(arguments.get("content", ""))
        if not content.strip():
            return ToolResult(ok=False, output="content is required")
        tags = [t.strip() for t in str(arguments.get("tags", "")).split(",") if t.strip()]
        entry = self._memory.add(content, tags=tags, source_session=self.session_id or "")
        return ToolResult(ok=True, output=f"remembered: {entry.id}")

    def _recall(self, arguments: dict) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, output="memory is disabled")
        query = str(arguments.get("query", ""))
        if not query.strip():
            return ToolResult(ok=False, output="query is required")
        top_k = arguments.get("top_k", 3)
        if type(top_k) is not int or top_k < 1:
            top_k = 3
        top_k = min(top_k, 10)
        entries = self._memory.recall(query, top_k=top_k)
        if not entries:
            return ToolResult(ok=True, output="(no relevant memories)")
        lines = [
            f"{i}. {e.content}" + (f" [tags: {', '.join(e.tags)}]" if e.tags else "")
            for i, e in enumerate(entries, 1)
        ]
        out, truncated = truncate("\n".join(lines))
        return ToolResult(ok=True, output=out, truncated=truncated)

    def _create_skill(self, arguments: dict) -> ToolResult:
        if self.skills is None:
            return ToolResult(ok=False, output="skills are not available")
        name = str(arguments.get("name", ""))
        description = str(arguments.get("description", ""))
        content = str(arguments.get("content", ""))
        if not name or not content:
            return ToolResult(ok=False, output="name and content are required")
        try:
            path = self.skills.add(name, description, content)
        except ValueError as e:
            return ToolResult(ok=False, output=f"invalid skill name: {e}")
        return ToolResult(ok=True, output=f"created skill: {name} ({path})")

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
                memory=False,
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
