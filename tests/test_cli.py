import os

import pytest

from code_agent.cli import _build_parser, _load_dotenv, _make_client, main
from code_agent.agent import RunResult


@pytest.fixture(autouse=True)
def _no_context_window_resolve(monkeypatch):
    monkeypatch.setattr("code_agent.cli.resolve_context_window", lambda *a, **k: 128000)


def test_load_dotenv_sets_missing_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nCODE_AGENT_BASE_URL="https://api.example.com/v1"\n'
        "CODE_AGENT_API_KEY='sk-secret'\nexport CODE_AGENT_MODEL=gpt-4o-mini\n"
        "MALFORMED_LINE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CODE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("CODE_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("CODE_AGENT_MODEL", raising=False)
    _load_dotenv(str(env_file))
    assert os.environ["CODE_AGENT_BASE_URL"] == "https://api.example.com/v1"
    assert os.environ["CODE_AGENT_API_KEY"] == "sk-secret"
    assert os.environ["CODE_AGENT_MODEL"] == "gpt-4o-mini"


def test_load_dotenv_does_not_override_existing_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CODE_AGENT_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_API_KEY", "from-env")
    _load_dotenv(str(env_file))
    assert os.environ["CODE_AGENT_API_KEY"] == "from-env"


def test_load_dotenv_missing_file(tmp_path):
    _load_dotenv(str(tmp_path / "does-not-exist.env"))
    assert True


def test_make_client_missing_key(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_API_KEY", raising=False)
    args = _build_parser().parse_args(["--prompt", "x"])
    with pytest.raises(SystemExit):
        _make_client(args)


def test_make_client_from_env(monkeypatch):
    monkeypatch.delenv("CODE_AGENT_MODEL", raising=False)
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    args = _build_parser().parse_args(["--prompt", "x"])
    client = _make_client(args)
    assert client.api_key == "test-key"
    assert client.model == "gpt-4o-mini"


def test_parser_defaults():
    args = _build_parser().parse_args(["--prompt", "x"])
    assert args.max_iterations == 20
    assert args.max_context_tokens == 90000
    assert args.context_window is None
    assert args.workdir == "."


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.store = kwargs.get("store")
        self.session_id = None

    def run_task(self, task, on_delta=None):
        on_delta("hello")
        return RunResult(final_text="hello", iterations=1, finished=True, reason="complete")

    def new_session(self):
        self.session_id = None

    def load_session(self, session_id):
        self.session_id = session_id


def test_main_oneshot(monkeypatch, capsys):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--prompt", "do it", "--workdir", "/tmp"])
    assert rc == 0
    assert "hello" in capsys.readouterr().out


def test_main_list_sessions(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("hello task")
    rc = main(["--list-sessions", "--workdir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert sid in out and "hello task" in out


def test_main_resume_not_found(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    rc = main(["--resume", "code_agent-nonexistent", "--prompt", "x", "--workdir", str(tmp_path)])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_main_interactive_slash_commands(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("existing")
    store.save(sid, [{"role": "user", "content": "hi"}])
    inputs = iter(["/list", f"/resume {sid}", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert sid in out and rc == 0


def test_main_interactive_shows_workspace(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    from code_agent.workspace import Workspace
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    sid = store.create("existing")
    store.save(sid, [{"role": "user", "content": "hi"}])
    Workspace(str(tmp_path)).touch_session(sid)
    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Workspace:" in out and "sessions: 1" in out and "last:" in out
    assert "Tip: /resume" in out and sid in out
    assert rc == 0


def test_main_interactive_no_tip_when_last_deleted(monkeypatch, capsys, tmp_path):
    from code_agent.session import SessionStore
    from code_agent.workspace import Workspace
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    store = SessionStore(str(tmp_path / ".code_agent" / "sessions"))
    Workspace(str(tmp_path)).touch_session("code_agent-gone")
    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "Workspace:" in out
    assert "Tip: /resume" not in out
    assert rc == 0


def test_main_oneshot_does_not_show_workspace(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--prompt", "do it", "--workdir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Workspace:" not in out


def test_main_policy_passed_and_rules_applied(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path),
               "--deny", "run_command:pytest *", "--allow", "read_file:*"])
    assert rc == 0
    policy = captured.get("policy")
    assert policy is not None
    assert captured.get("interact") is False
    assert policy.check("run_command", {"command": "pytest tests/"}).decision == "deny"
    assert policy.check("read_file", {"path": "a"}).decision == "allow"


def test_main_passes_skill_registry_when_skills_present(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.skills._default_user_dir", lambda: str(tmp_path / "user-skills"))
    proj = str(tmp_path)
    skill_dir = os.path.join(proj, ".code_agent", "skills", "greeting")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: greeting\ndescription: 用中文问候\n---\n技能已加载：greeting\n")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", proj])
    assert rc == 0
    skills = captured.get("skills")
    assert skills is not None
    assert [s.name for s in skills.scan()] == ["greeting"]


def test_main_no_skills_passes_none(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.skills._default_user_dir", lambda: str(tmp_path / "user-skills"))
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path)])
    assert rc == 0
    assert captured.get("skills") is None


def test_main_interactive_policy_interact(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def new_session(self):
            pass

        def load_session(self, sid):
            pass

    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--interactive", "--workdir", str(tmp_path)])
    assert rc == 0
    assert captured.get("interact") is True


def test_use_tui_tty(monkeypatch):
    from code_agent.cli import _use_tui
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("NO_TUI", raising=False)
    assert _use_tui() is True


def test_use_tui_not_tty(monkeypatch):
    from code_agent.cli import _use_tui
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.delenv("NO_TUI", raising=False)
    assert _use_tui() is False


def test_use_tui_no_tui_env(monkeypatch):
    from code_agent.cli import _use_tui
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("NO_TUI", "1")
    assert _use_tui() is False


def test_handle_command_exit_and_unknown():
    from code_agent.cli import handle_command
    keep, out = handle_command("/exit", None, None)
    assert keep is False and out == []
    keep, out = handle_command("/bogus", None, None)
    assert keep is True and out == ["unknown command: /bogus"]
    keep, out = handle_command("/resume", None, None)
    assert keep is True and out == ["usage: /resume <session-id>"]


def test_handle_command_new_and_list(workdir, tmp_path):
    from code_agent.cli import handle_command
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("t")

    class _S:
        def new_session(self):
            self.called = True

    s = _S()
    keep, out = handle_command("/new", s, store)
    assert keep is True and getattr(s, "called", False) and out == ["New session started."]

    keep, out = handle_command("/list", None, store)
    assert keep is True and out and sid in out[0]


def test_handle_command_resume(workdir, tmp_path):
    from code_agent.cli import handle_command
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("t")

    class _S:
        def load_session(self, sid):
            self.loaded = sid

    s = _S()
    keep, out = handle_command(f"/resume {sid}", s, store)
    assert keep is True and s.loaded == sid and out == [f"Resumed session {sid}."]


def test_main_passes_context_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    captured = {}

    class _CaptureSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_task(self, task, on_delta=None):
            return RunResult(final_text="ok", iterations=1, finished=True, reason="complete")

    monkeypatch.setattr("code_agent.cli.AgentSession", _CaptureSession)
    rc = main(["--prompt", "x", "--workdir", str(tmp_path), "--context-window", "64000"])
    assert rc == 0
    assert captured.get("context_window") == 64000


def test_handle_command_rename(workdir, tmp_path):
    from code_agent.cli import handle_command
    from code_agent.session import SessionStore
    store = SessionStore(str(tmp_path / "sessions"))
    sid = store.create("auto")

    class _S:
        def __init__(self):
            self.session_id = sid
            self.called = None

        def rename_session(self, title):
            self.called = title
            return title

    s = _S()
    keep, out = handle_command("/rename my title", s, store)
    assert keep is True and s.called == "my title" and out == ["renamed: my title"]
    keep, out = handle_command("/rename", s, store)
    assert keep is True and out == ["usage: /rename <title>"]
