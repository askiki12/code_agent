"""Message history, token estimation, and trimming."""
from __future__ import annotations

import json
import re
from typing import Any

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate: CJK chars ~1 token, other chars ~1 token / 4 chars."""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, cjk + other // 4)


class Conversation:
    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []

    def add_system(self, text: str) -> None:
        self._messages.append({"role": "system", "content": text})

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def add_assistant(self, content: str, tool_calls: list | None = None) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]
        self._messages.append(msg)

    def add_tool(self, tool_call_id: str, name: str, output: str) -> None:
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": output,
            }
        )

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def is_valid(self) -> bool:
        """True if no orphan tool message: every tool msg has a preceding assistant tool_call."""
        pending: set[str] = set()
        for msg in self._messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                pending.update(tc["id"] for tc in msg["tool_calls"])
            elif msg["role"] == "tool":
                if msg.get("tool_call_id") not in pending:
                    return False
                pending.discard(msg["tool_call_id"])
        return True

    def to_jsonl(self) -> str:
        lines = [json.dumps(m, ensure_ascii=False) for m in self._messages]
        return "\n".join(lines) + "\n" if lines else ""

    @classmethod
    def from_jsonl(cls, text: str, system_prompt: str | None = None) -> "Conversation":
        conv = cls()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or "role" not in obj:
                continue
            conv._messages.append(obj)
        if system_prompt is not None:
            conv._messages = [m for m in conv._messages if m.get("role") != "system"]
            conv._messages.insert(0, {"role": "system", "content": system_prompt})
        return conv

    def build_messages(self, max_tokens: int) -> list[dict[str, Any]]:
        system = [m for m in self._messages if m["role"] == "system"]
        rest = [m for m in self._messages if m["role"] != "system"]
        out = list(system)
        budget = max_tokens - sum(
            estimate_tokens(str(m.get("content", ""))) for m in system
        )
        if budget <= 0:
            return out
        groups = self._group(rest)
        selected: list[dict[str, Any]] = []
        for group in reversed(groups):
            cost = sum(estimate_tokens(str(m.get("content", ""))) for m in group)
            if budget - cost < 0:
                if not selected:
                    selected = self._truncate_group(group, budget)
                break
            budget -= cost
            selected = group + selected
        return out + selected

    @staticmethod
    def _group(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Bundle each assistant (with tool_calls) with its following tool messages."""
        groups: list[list[dict[str, Any]]] = []
        i, n = 0, len(messages)
        while i < n:
            msg = messages[i]
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                group = [msg]
                i += 1
                while i < n and messages[i]["role"] == "tool":
                    group.append(messages[i])
                    i += 1
                groups.append(group)
            else:
                groups.append([msg])
                i += 1
        return groups

    @staticmethod
    def _truncate_group(
        group: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        out = []
        for m in group:
            m = dict(m)
            if m["role"] == "tool":
                content = str(m.get("content", ""))
                if len(content) > budget:
                    m["content"] = content[:budget] + f"\n...[truncated {len(content) - budget} chars]"
            out.append(m)
        return out
