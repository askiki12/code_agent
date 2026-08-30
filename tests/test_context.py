from code_agent.context import Conversation, estimate_tokens


class _TC:
    def __init__(self, id_, name, args):
        self.id = id_
        self.name = name
        self.arguments = args


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 1


def test_estimate_tokens_ascii():
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_cjk():
    assert estimate_tokens("你好世界") == 4


def test_add_messages_order():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    assert [m["role"] for m in conv.messages] == ["system", "user"]


def test_is_valid_true():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    conv.add_assistant("", [_TC("c1", "read_file", {})])
    conv.add_tool("c1", "read_file", "ok")
    assert conv.is_valid()


def test_is_valid_false_orphan_tool():
    conv = Conversation()
    conv.add_tool("orphan", "read_file", "x")
    assert not conv.is_valid()


def test_build_messages_keeps_system_and_order():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u1")
    conv.add_assistant("a1")
    conv.add_user("u2")
    msgs = conv.build_messages(100000)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "sys"


def test_build_messages_trims_old_when_over_budget():
    conv = Conversation()
    conv.add_system("sys")
    for i in range(10):
        conv.add_user(f"user message {i} " + "x" * 50)
        conv.add_assistant(f"assistant reply {i} " + "y" * 50)
    msgs = conv.build_messages(200)
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["content"].startswith("assistant reply 9")
    assert not any("assistant reply 0" in m["content"] for m in msgs)


def test_build_messages_grouping_no_orphan_tool():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    for i in range(5):
        conv.add_assistant("", [_TC(f"c{i}", "read_file", {})])
        conv.add_tool(f"c{i}", "read_file", "result " + "z" * 100)
    msgs = conv.build_messages(300)
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            assert msgs[i - 1]["role"] == "assistant"


def test_to_jsonl_from_jsonl_roundtrip():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u")
    conv.add_assistant("", [_TC("c1", "read_file", {"path": "a"})])
    conv.add_tool("c1", "read_file", "ok")
    text = conv.to_jsonl()
    restored = Conversation.from_jsonl(text)
    assert restored.messages == conv.messages
    assert restored.is_valid()


def test_from_jsonl_reinjects_system():
    conv = Conversation()
    conv.add_system("old-system")
    conv.add_user("u")
    text = conv.to_jsonl()
    restored = Conversation.from_jsonl(text, system_prompt="new-system")
    assert restored.messages[0] == {"role": "system", "content": "new-system"}
    assert sum(1 for m in restored.messages if m["role"] == "system") == 1


def test_from_jsonl_skips_bad_lines():
    conv = Conversation.from_jsonl('not-json\n{}\n{"role":"user","content":"u"}\n')
    assert conv.messages == [{"role": "user", "content": "u"}]


def test_add_assistant_cleans_surrogates():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_assistant("bad \ud83d text")
    content = conv.messages[-1]["content"]
    assert "\ud83d" not in content and "\ufffd" in content


def test_add_user_and_tool_clean_surrogates():
    conv = Conversation()
    conv.add_system("sys")
    conv.add_user("u \ud83e")
    conv.add_tool("c1", "run_command", "out \ud83d")
    assert "\ud83e" not in conv.messages[1]["content"]
    assert "\ud83d" not in conv.messages[2]["content"]
