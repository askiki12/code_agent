"""Agent session loop, termination, and error recovery."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from code_agent.context import Conversation
from code_agent.llm import LLMError
from code_agent.tools import TOOL_SCHEMAS, ToolResult, execute

SYSTEM_PROMPT = """You are a coding agent. You work inside a local workspace and complete software tasks autonomously.

Available tools:
- read_file: read text files
- write_file: create or overwrite files
- edit_file: replace an exact substring in a file (must match uniquely)
- list_dir: list directory contents
- run_command: run a shell command (has a timeout)

Rules:
1. Plan each step. Prefer small, verifiable changes.
2. Use run_command to verify your work (e.g. run tests).
3. When a tool fails, read the error, adjust, and retry. Do not repeat the exact same failing call.
4. Do NOT read or write protected paths such as .env, .env.* or .git.
5. When the task is complete, reply with a short final summary and stop making tool calls.
"""

MAX_ITERATIONS_DEFAULT = 20
MAX_CONSECUTIVE_FAILURES = 3


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
    ) -> None:
        self.workdir = workdir
        self.llm = llm
        self.max_iterations = max_iterations
        self.max_context_tokens = max_context_tokens
        self.debug = debug
        self.conversation = Conversation()
        self.conversation.add_system(SYSTEM_PROMPT)

    def run_task(self, task: str, on_delta: Callable[[str], None] | None = None) -> RunResult:
        self.conversation.add_user(task)
        consecutive_failures = 0
        llm_error_count = 0
        for iteration in range(1, self.max_iterations + 1):
            if self.debug:
                print(f"[agent] iteration {iteration}")
            messages = self.conversation.build_messages(self.max_context_tokens)
            try:
                response = self.llm.chat(messages, tools=TOOL_SCHEMAS, on_delta=on_delta)
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
                result = self._run_tool(tc)
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

    def _run_tool(self, tc) -> ToolResult:
        try:
            return execute(tc.name, tc.arguments, self.workdir)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return ToolResult(ok=False, output=f"tool crash: {type(e).__name__}: {e}")
