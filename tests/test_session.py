import os

import pytest

from code_agent.session import SessionStore, _make_title


def test_create_writes_meta_file(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("hello task")
    assert os.path.isfile(os.path.join(str(tmp_path), f"{sid}.jsonl"))
    meta, msgs = store.load(sid)
    assert meta["type"] == "meta" and meta["title"] == "hello task"
    assert msgs == []


def test_save_load_roundtrip(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "ok"},
    ]
    store.save(sid, messages)
    meta, loaded = store.load(sid)
    assert loaded == messages
    assert meta["message_count"] == 3


def test_list_sessions_sorted_by_updated(tmp_path):
    store = SessionStore(str(tmp_path))
    a = store.create("first")
    b = store.create("second")
    store.save(b, [{"role": "user", "content": "b2"}])
    sessions = store.list_sessions()
    assert [s["id"] for s in sessions] == [b, a]
    assert sessions[0]["message_count"] == 1
    assert sessions[1]["message_count"] == 0


def test_list_sessions_missing_dir(tmp_path):
    store = SessionStore(str(tmp_path / "nope"))
    assert store.list_sessions() == []


def test_load_missing_raises_keyerror(tmp_path):
    store = SessionStore(str(tmp_path))
    with pytest.raises(KeyError):
        store.load("code_agent-20260829-000000")


def test_load_skips_corrupt_lines(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = "code_agent-test"
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"type":"meta","id":"' + sid + '"}\n')
        f.write("garbage-line\n")
        f.write('{"role":"user","content":"ok"}\n')
    meta, msgs = store.load(sid)
    assert meta["id"] == sid
    assert msgs == [{"role": "user", "content": "ok"}]


def test_load_skips_non_dict_lines(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = "code_agent-test-nd"
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"type":"meta","id":"' + sid + '"}\n')
        f.write('["not","a","dict"]\n')
        f.write('{"role":"user","content":"ok"}\n')
    meta, msgs = store.load(sid)
    assert meta["id"] == sid
    assert msgs == [{"role": "user", "content": "ok"}]


def test_save_keeps_created_at(tmp_path):
    store = SessionStore(str(tmp_path))
    sid = store.create("t")
    meta0, _ = store.load(sid)
    store.save(sid, [{"role": "user", "content": "x"}])
    meta1, _ = store.load(sid)
    assert meta1["created_at"] == meta0["created_at"]
    assert meta1["message_count"] == 1


def test_make_title_truncates():
    assert _make_title("a" * 100) == "a" * 40
    assert _make_title("  hi   there  ") == "hi there"


def test_save_conversation_with_surrogates(tmp_path):
    from code_agent.context import Conversation
    store = SessionStore(str(tmp_path / "sessions"))
    conv = Conversation()
    conv.add_system("sys")
    conv.add_assistant("bad \ud83d content")
    sid = store.create("t")
    store.save(sid, conv.messages, title="t")  # must not raise
    _, msgs = store.load(sid)
    assert "surrogate" not in str(msgs)
