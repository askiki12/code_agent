"""Workspace identity and metadata (first-class working directory)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime


def _make_workspace_id(workdir: str) -> str:
    real = os.path.realpath(workdir)
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


class Workspace:
    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self._dir = os.path.join(workdir, ".code_agent")
        self._path = os.path.join(self._dir, "workspace.json")
        self._data = self._load_or_init()

    def _load_or_init(self) -> dict:
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if self._valid(data):
                    return data
                print("[workspace] warning: invalid workspace.json, rebuilding", file=sys.stderr)
            except (OSError, json.JSONDecodeError) as e:
                print(f"[workspace] warning: failed to read workspace.json ({e}), rebuilding", file=sys.stderr)
        return self._create()

    @staticmethod
    def _valid(data) -> bool:
        return isinstance(data, dict) and all(k in data for k in ("id", "name", "path"))

    def _create(self) -> dict:
        os.makedirs(self._dir, exist_ok=True)
        real = os.path.realpath(self.workdir)
        now = _now()
        data = {
            "id": _make_workspace_id(self.workdir),
            "name": os.path.basename(real) or real,
            "path": real,
            "created_at": now,
            "updated_at": now,
        }
        self._write(data)
        return data

    def _write(self, data: dict) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self._path)

    @property
    def data(self) -> dict:
        return dict(self._data)

    @property
    def id(self) -> str:
        return self._data["id"]

    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def path(self) -> str:
        return self._data["path"]

    @property
    def last_session_id(self) -> str | None:
        return self._data.get("last_session_id")

    def touch_session(self, session_id: str) -> None:
        self._data["last_session_id"] = session_id
        self._data["updated_at"] = _now()
        self._write(self._data)

    def display(self) -> str:
        return f"Workspace: {self.name} ({self.id})"
