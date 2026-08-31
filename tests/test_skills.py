import os

import pytest

from code_agent.skills import SkillRegistry, _parse_frontmatter


def _make_skill(root, name, desc, body):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")


def test_parse_frontmatter_valid():
    name, desc, body = _parse_frontmatter(
        "---\nname: code-review\ndescription: do review\n---\nsteps here"
    )
    assert (name, desc) == ("code-review", "do review")
    assert body == "steps here"


def test_parse_frontmatter_invalid():
    assert _parse_frontmatter("no frontmatter") is None
    assert _parse_frontmatter("---\nname: x\n---\nbody") is None
    assert _parse_frontmatter("---\ndescription: x\n---\nbody") is None
    assert _parse_frontmatter("---\nname: bad name\ndescription: x\n---\nbody") is None


def test_scan_merges_and_sorts(tmp_path):
    proj = str(tmp_path / "proj")
    user = str(tmp_path / "user")
    _make_skill(os.path.join(proj, ".code_agent", "skills"), "zebra", "z skill", "z body")
    _make_skill(user, "alpha", "a skill", "a body")
    reg = SkillRegistry(proj, user)
    skills = reg.scan()
    assert [s.name for s in skills] == ["alpha", "zebra"]


def test_scan_project_overrides_user(tmp_path):
    proj = str(tmp_path / "proj")
    user = str(tmp_path / "user")
    _make_skill(os.path.join(proj, ".code_agent", "skills"), "dup", "project version", "project body")
    _make_skill(user, "dup", "user version", "user body")
    reg = SkillRegistry(proj, user)
    skills = reg.scan()
    assert len(skills) == 1
    assert skills[0].description == "project version"


def test_load_returns_content(tmp_path):
    proj = str(tmp_path / "proj")
    _make_skill(os.path.join(proj, ".code_agent", "skills"), "code-review", "do review", "step1\nstep2")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    content = reg.load("code-review")
    assert content and "step1" in content and "name: code-review" in content


def test_load_missing_returns_none(tmp_path):
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    assert reg.load("nope") is None


def test_scan_missing_dirs_empty(tmp_path):
    reg = SkillRegistry(str(tmp_path / "nope-proj"), str(tmp_path / "nope-user"))
    assert reg.scan() == []


def test_scan_skips_name_mismatch(tmp_path, capsys):
    proj = str(tmp_path / "proj")
    d = os.path.join(proj, ".code_agent", "skills", "foo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: bar\ndescription: mismatch\n---\nbody\n")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    assert reg.scan() == []
    assert "name 'bar' != directory 'foo'" in capsys.readouterr().err


def test_load_rejects_path_traversal(tmp_path):
    proj = str(tmp_path / "proj")
    d = os.path.join(proj, ".code_agent", "evil")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: evil\ndescription: evil\n---\nbody\n")
    reg = SkillRegistry(proj, str(tmp_path / "user"))
    assert reg.load("../evil") is None
    assert reg.load("a/b") is None


def test_skill_registry_add(tmp_path):
    import os
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    path = reg.add("build", "build and test", "1. run uv sync\n2. run uv run pytest")
    assert os.path.isfile(path)
    names = [s.name for s in reg.scan()]
    assert "build" in names
    content = reg.load("build")
    assert "uv run pytest" in content


def test_skill_registry_add_invalid_name(tmp_path):
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    with pytest.raises(ValueError):
        reg.add("../evil", "d", "c")
    with pytest.raises(ValueError):
        reg.add("has space", "d", "c")


def test_skill_registry_add_overwrites(tmp_path):
    reg = SkillRegistry(str(tmp_path / "proj"), str(tmp_path / "user"))
    reg.add("build", "v1", "old")
    reg.add("build", "v2", "new")
    assert "new" in reg.load("build")
