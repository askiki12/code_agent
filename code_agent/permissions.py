"""Permission policy: allow/ask/deny rules, readonly whitelist, doom-loop detection."""
from __future__ import annotations

import fnmatch
import json
import sys
from collections import deque
from dataclasses import dataclass

DOOM_LOOP_LIMIT = 3

READONLY_PREFIXES = [
    "ls", "cat", "head", "tail", "grep", "rg", "pwd", "whoami",
    "echo", "date", "find", "wc", "file",
    "python3 -V", "python -V",
    "git status", "git log", "git diff", "git show", "git branch",
]

_SHELL_OPS = (";", "|", "&&", "||", ">", "<", ">>", "&", "`", "$(")


@dataclass
class PermissionResult:
    decision: str
    reason: str | None = None


def parse_rule(rule: str) -> tuple[str, str] | None:
    tool, _, pattern = rule.partition(":")
    tool = tool.strip()
    pattern = pattern.strip()
    if not tool or not pattern:
        return None
    return tool, pattern


def _match_text(tool: str, arguments: dict) -> str:
    if tool == "run_command":
        return str(arguments.get("command", ""))
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _is_readonly_command(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    for op in _SHELL_OPS:
        if op in stripped:
            return False
    lower = stripped.lower()
    return any(lower.startswith(prefix) for prefix in READONLY_PREFIXES)


class Policy:
    def __init__(
        self,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        ask: list[str] | None = None,
    ) -> None:
        self._allow = self._parse_rules(allow)
        self._deny = self._parse_rules(deny)
        self._ask = self._parse_rules(ask)
        self._calls: deque[tuple[str, str]] = deque(maxlen=DOOM_LOOP_LIMIT)

    @staticmethod
    def _parse_rules(rules: list[str] | None) -> list[tuple[str, str]]:
        parsed: list[tuple[str, str]] = []
        for rule in rules or []:
            x = parse_rule(rule)
            if x is None:
                print(f"[permission] warning: invalid rule ignored: {rule}", file=sys.stderr)
            else:
                parsed.append(x)
        return parsed

    def _matches(self, rules: list[tuple[str, str]], tool: str, text: str) -> bool:
        for rule_tool, pattern in rules:
            if rule_tool != tool:
                continue
            if fnmatch.fnmatch(text, pattern):
                return True
            if pattern.endswith(" *") and fnmatch.fnmatch(text, pattern[:-2]):
                return True
        return False

    def _same_streak(self, key: tuple[str, str]) -> int:
        streak = 0
        for k in reversed(self._calls):
            if k == key:
                streak += 1
            else:
                break
        return streak

    def would_loop(self, tool: str, arguments: dict) -> bool:
        key = (tool, _match_text(tool, arguments))
        return self._same_streak(key) >= DOOM_LOOP_LIMIT - 1

    def check(self, tool: str, arguments: dict, interact: bool = False) -> PermissionResult:
        text = _match_text(tool, arguments)
        key = (tool, text)
        if self.would_loop(tool, arguments):
            self._calls.append(key)
            return PermissionResult("deny", "doom_loop detected: repeated tool call")
        if self._matches(self._deny, tool, text):
            self._calls.append(key)
            return PermissionResult("deny", "denied by rule")
        if self._matches(self._ask, tool, text):
            if interact:
                prompt = f"[permission] {tool}({text[:60]!r}) allowed? [y/N] "
                decision = "allow" if input(prompt).strip().lower() == "y" else "deny"
            else:
                decision = "deny"
            self._calls.append(key)
            return PermissionResult(decision, "ask rule")
        if self._matches(self._allow, tool, text):
            self._calls.append(key)
            return PermissionResult("allow")
        if tool == "run_command" and _is_readonly_command(text):
            self._calls.append(key)
            return PermissionResult("allow", "readonly whitelist")
        self._calls.append(key)
        return PermissionResult("allow")
