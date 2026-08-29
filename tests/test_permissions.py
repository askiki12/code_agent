from code_agent.permissions import Policy, parse_rule, _is_readonly_command


def test_parse_rule_valid():
    assert parse_rule("run_command:git status") == ("run_command", "git status")


def test_parse_rule_invalid():
    assert parse_rule("no-colon") is None
    assert parse_rule("tool:") is None


def test_check_default_allow():
    p = Policy()
    assert p.check("read_file", {"path": "a.txt"}).decision == "allow"


def test_check_deny_rule():
    p = Policy(deny=["run_command:pytest *"])
    assert p.check("run_command", {"command": "pytest tests/"}).decision == "deny"


def test_check_allow_rule():
    p = Policy(allow=["run_command:git *"])
    assert p.check("run_command", {"command": "git status"}).decision == "allow"


def test_deny_beats_allow():
    p = Policy(allow=["run_command:*"], deny=["run_command:pytest *"])
    assert p.check("run_command", {"command": "pytest x"}).decision == "deny"
    assert p.check("run_command", {"command": "ls"}).decision == "allow"


def test_ask_interactive_yes(monkeypatch):
    p = Policy(ask=["run_command:git push *"])
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert p.check("run_command", {"command": "git push"}, interact=True).decision == "allow"


def test_ask_interactive_no(monkeypatch):
    p = Policy(ask=["run_command:git push *"])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert p.check("run_command", {"command": "git push"}, interact=True).decision == "deny"


def test_ask_noninteractive_deny():
    p = Policy(ask=["run_command:git push *"])
    assert p.check("run_command", {"command": "git push"}, interact=False).decision == "deny"


def test_readonly_whitelist():
    p = Policy()
    assert p.check("run_command", {"command": "ls"}, interact=True).decision == "allow"
    assert p.check("run_command", {"command": "git status"}, interact=True).decision == "allow"
    assert _is_readonly_command("ls -la") is True
    assert _is_readonly_command("ls -la; rm -rf /") is False
    assert _is_readonly_command("echo hi > out.txt") is False


def test_would_loop():
    p = Policy()
    assert not p.would_loop("run_command", {"command": "echo hi"})
    p.check("run_command", {"command": "echo hi"})
    p.check("run_command", {"command": "echo hi"})
    assert p.would_loop("run_command", {"command": "echo hi"})
    assert p.check("run_command", {"command": "echo hi"}).decision == "deny"
    p.check("run_command", {"command": "echo bye"})
    assert p.check("run_command", {"command": "echo hi"}).decision == "allow"


def test_pattern_matches_command_text():
    p = Policy(deny=["run_command:git *"])
    assert p.check("run_command", {"command": "git status"}).decision == "deny"
    assert p.check("run_command", {"command": "git log"}).decision == "deny"


def test_trailing_wildcard_matches_bare_command():
    p = Policy(deny=["run_command:git push *"])
    assert p.check("run_command", {"command": "git push"}).decision == "deny"
    assert p.check("run_command", {"command": "git push origin main"}).decision == "deny"
    assert p.check("run_command", {"command": "git status"}).decision == "allow"
