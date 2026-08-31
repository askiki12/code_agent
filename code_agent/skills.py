"""Skill registry: scan and load SKILL.md skills from project and user dirs."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass


@dataclass
class Skill:
    name: str
    description: str
    path: str


def _parse_frontmatter(text: str) -> tuple[str, str, str] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    _, front, body = parts
    name = None
    description = None
    for line in front.strip().splitlines():
        if line.startswith("name:"):
            name = line[len("name:"):].strip()
        elif line.startswith("description:"):
            description = line[len("description:"):].strip()
    if not name or not description:
        return None
    if any(c.isspace() for c in name):
        return None
    return name, description, body.lstrip("\n")


def _default_user_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".code_agent", "skills")


class SkillRegistry:
    def __init__(self, project_dir: str, user_dir: str | None = None) -> None:
        self._project_dir = os.path.join(project_dir, ".code_agent", "skills")
        self._user_dir = user_dir or _default_user_dir()

    @staticmethod
    def _list_dirs(base: str) -> list[str]:
        if not os.path.isdir(base):
            return []
        try:
            return sorted(os.listdir(base))
        except OSError:
            return []

    def _read_skill(self, base: str, name: str) -> Skill | None:
        path = os.path.join(base, name, "SKILL.md")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"[skills] warning: failed to read {path}: {e}", file=sys.stderr)
            return None
        parsed = _parse_frontmatter(text)
        if parsed is None:
            print(f"[skills] warning: invalid SKILL.md skipped: {path}", file=sys.stderr)
            return None
        sname, desc, _ = parsed
        if sname != name:
            print(f"[skills] warning: SKILL.md name '{sname}' != directory '{name}', skipped: {path}", file=sys.stderr)
            return None
        return Skill(name=sname, description=desc, path=path)

    def scan(self) -> list[Skill]:
        by_name: dict[str, Skill] = {}
        for base in (self._user_dir, self._project_dir):
            for name in self._list_dirs(base):
                skill = self._read_skill(base, name)
                if skill is not None:
                    by_name[skill.name] = skill
        return sorted(by_name.values(), key=lambda s: s.name)

    def load(self, name: str) -> str | None:
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        for base in (self._project_dir, self._user_dir):
            path = os.path.join(base, name, "SKILL.md")
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read()
                except OSError as e:
                    print(f"[skills] warning: failed to read {path}: {e}", file=sys.stderr)
                    return None
        return None

    def add(self, name: str, description: str, content: str) -> str:
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"invalid skill name: {name!r}")
        directory = os.path.join(self._project_dir, name)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "SKILL.md")
        desc_line = " ".join(description.split()) or name
        text = f"---\nname: {name}\ndescription: {desc_line}\n---\n{content}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
