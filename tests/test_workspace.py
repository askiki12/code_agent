import os

from code_agent.workspace import Workspace, _make_workspace_id


def test_init_creates_workspace_file(tmp_path):
    w = Workspace(str(tmp_path))
    assert os.path.isfile(os.path.join(str(tmp_path), ".code_agent", "workspace.json"))
    assert w.name == os.path.basename(str(tmp_path))


def test_init_idempotent_preserves_created_at(tmp_path):
    w1 = Workspace(str(tmp_path))
    w2 = Workspace(str(tmp_path))
    assert w1.data["created_at"] == w2.data["created_at"]
    assert w1.id == w2.id


def test_id_is_stable_hash(tmp_path):
    w1 = Workspace(str(tmp_path))
    w2 = Workspace(str(tmp_path))
    assert w1.id == w2.id == _make_workspace_id(str(tmp_path))
    assert len(w1.id) == 12


def test_touch_session_updates_last_and_updated_at(tmp_path):
    w = Workspace(str(tmp_path))
    before = w.data["updated_at"]
    w.touch_session("code_agent-1")
    assert w.last_session_id == "code_agent-1"
    assert w.data["updated_at"] != before


def test_corrupt_json_rebuilds(tmp_path, capsys):
    p = os.path.join(str(tmp_path), ".code_agent", "workspace.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("not-json")
    w = Workspace(str(tmp_path))
    assert "warning" in capsys.readouterr().err
    assert w.data["id"] == _make_workspace_id(str(tmp_path))


def test_display_contains_name_and_id(tmp_path):
    w = Workspace(str(tmp_path))
    assert w.display() == f"Workspace: {w.name} ({w.id})"
