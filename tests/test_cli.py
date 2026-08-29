import os

import pytest

from code_agent.cli import _build_parser, _load_dotenv, _make_client, main
from code_agent.agent import RunResult


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
    assert args.workdir == "."


class _FakeSession:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_task(self, task, on_delta=None):
        on_delta("hello")
        return RunResult(final_text="hello", iterations=1, finished=True, reason="complete")


def test_main_oneshot(monkeypatch, capsys):
    monkeypatch.setenv("CODE_AGENT_API_KEY", "test-key")
    monkeypatch.setattr("code_agent.cli.AgentSession", _FakeSession)
    rc = main(["--prompt", "do it", "--workdir", "/tmp"])
    assert rc == 0
    assert "hello" in capsys.readouterr().out
