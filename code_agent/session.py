"""Session persistence: JSONL storage of conversation messages."""
from __future__ import annotations

import json
import os
from datetime import datetime

TITLE_MAX_CHARS = 40


def _make_session_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("code_agent-%Y%m%d-%H%M%S%f")


def _make_title(task: str) -> str:
    return " ".join(task.split())[:TITLE_MAX_CHARS]


def _meta_dict(session_id: str, title: str, created_at: str, updated_at: str, message_count: int) -> dict:
    return {
        "type": "meta",
        "id": session_id,
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "message_count": message_count,
    }


class SessionStore:
    def __init__(self, root: str) -> None:
        self.root = root

    @staticmethod
    def _path(root: str, session_id: str) -> str:
        return os.path.join(root, f"{session_id}.jsonl")

    def _read_meta(self, path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
        except OSError:
            return None
        if not first:
            return None
        try:
            obj = json.loads(first)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict) and obj.get("type") == "meta":
            return obj
        return None

    def list_sessions(self) -> list[dict]:
        if not os.path.isdir(self.root):
            return []
        out: list[dict] = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".jsonl"):
                continue
            meta = self._read_meta(os.path.join(self.root, name))
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def create(self, title: str) -> str:
        os.makedirs(self.root, exist_ok=True)
        session_id = _make_session_id()
        now = datetime.now().isoformat(timespec="microseconds")
        meta = _meta_dict(session_id, title, now, now, 0)
        self._write(self._path(self.root, session_id), [meta])
        return session_id

    def save(self, session_id: str, messages: list[dict], title: str | None = None) -> None:
        path = self._path(self.root, session_id)
        existing = self._read_meta(path) if os.path.isfile(path) else None
        now = datetime.now().isoformat(timespec="microseconds")
        created_at = existing["created_at"] if existing else now
        pinned = existing is not None and existing.get("title_pinned")
        if existing is None:
            os.makedirs(self.root, exist_ok=True)
        if pinned:
            resolved_title = existing.get("title") or ""
        elif title is not None:
            resolved_title = title
        else:
            resolved_title = (existing.get("title") or "") if existing else ""
        meta = _meta_dict(session_id, resolved_title, created_at, now, len(messages))
        if pinned:
            meta["title_pinned"] = True
        self._write(path, [meta] + list(messages))

    def load(self, session_id: str) -> tuple[dict, list[dict]]:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            raise KeyError(session_id)
        meta: dict | None = None
        messages: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "meta":
                    meta = obj
                    continue
                messages.append(obj)
        if meta is None:
            raise KeyError(session_id)
        return meta, messages

    def _write(self, path: str, objects: list[dict]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for obj in objects:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def _write_lines(self, path: str, lines: list[str]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)

    def get_title(self, session_id: str) -> str:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            return ""
        meta = self._read_meta(path)
        if meta is None:
            return ""
        return meta.get("title") or ""

    def rename(self, session_id: str, title: str) -> None:
        path = self._path(self.root, session_id)
        if not os.path.isfile(path):
            raise KeyError(session_id)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        meta_line = lines[0] if lines else ""
        try:
            meta = json.loads(meta_line)
        except json.JSONDecodeError:
            raise KeyError(session_id) from None
        if not isinstance(meta, dict) or meta.get("type") != "meta":
            raise KeyError(session_id)
        meta["title"] = title
        meta["title_pinned"] = True
        meta["updated_at"] = datetime.now().isoformat(timespec="microseconds")
        lines[0] = json.dumps(meta, ensure_ascii=False)
        self._write_lines(path, lines)
