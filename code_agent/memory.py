"""Project memory: cross-session knowledge store with keyword retrieval."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

_MEM_ID_PATTERN = "code_agent-mem-%Y%m%d-%H%M%S%f"
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    source_session: str = ""
    created_at: str = ""
    updated_at: str = ""
    usage_count: int = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _make_id() -> str:
    return datetime.now().strftime(_MEM_ID_PATTERN)


def _tokens(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    cjk = [ch for ch in text if _CJK_RE.match(ch)]
    return words + cjk


def _score(entry: MemoryEntry, query_tokens: list[str]) -> int:
    entry_tokens = set(_tokens(entry.content))
    if not entry_tokens:
        return 0
    return sum(len(t) for t in query_tokens if t in entry_tokens)


class MemoryStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self._entries: list[MemoryEntry] = []
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, "memories.jsonl")

    def _load(self) -> None:
        if not os.path.isfile(self._path()):
            return
        try:
            with open(self._path(), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or "content" not in obj:
                        continue
                    tags = obj.get("tags", []) or []
                    if not isinstance(tags, list):
                        tags = []
                    usage_count = obj.get("usage_count", 0)
                    if not isinstance(usage_count, int):
                        usage_count = 0
                    self._entries.append(
                        MemoryEntry(
                            id=obj.get("id", ""),
                            content=obj["content"],
                            tags=[str(t) for t in tags],
                            source_session=obj.get("source_session", "") or "",
                            created_at=obj.get("created_at", "") or "",
                            updated_at=obj.get("updated_at", "") or "",
                            usage_count=usage_count,
                        )
                    )
        except OSError:
            pass

    def _save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(
                    json.dumps(
                        {
                            "id": e.id,
                            "content": e.content,
                            "tags": e.tags,
                            "source_session": e.source_session,
                            "created_at": e.created_at,
                            "updated_at": e.updated_at,
                            "usage_count": e.usage_count,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.replace(tmp, self._path())

    def all(self) -> list[MemoryEntry]:
        return list(self._entries)

    def add(self, content: str, tags: list[str] | None = None, source_session: str = "") -> MemoryEntry:
        now = _now()
        entry = MemoryEntry(
            id=_make_id(),
            content=content,
            tags=list(tags or []),
            source_session=source_session,
            created_at=now,
            updated_at=now,
        )
        self._entries.append(entry)
        self._save()
        return entry

    def recall(self, query: str, top_k: int = 3) -> list[MemoryEntry]:
        query_tokens = _tokens(query)
        scored = [(_score(e, query_tokens), e) for e in self._entries]
        hits = [(s, e) for s, e in scored if s > 0]
        hits.sort(key=lambda pair: (-pair[0], -pair[1].usage_count))
        selected = [e for _, e in hits[:top_k]]
        if selected:
            for e in selected:
                e.usage_count += 1
            self._save()
        return selected
