import os

import pytest

from code_agent.memory import MemoryStore, _tokens


def test_add_and_all(tmp_path):
    store = MemoryStore(str(tmp_path))
    e = store.add("the project uses uv for the environment", source_session="s1")
    assert e.id.startswith("code_agent-mem-")
    assert store.all() == [e]


def test_recall_relevant(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("the project uses uv for the environment")
    store.add("the api key lives in .env")
    hits = store.recall("uv environment", top_k=1)
    assert len(hits) == 1
    assert "uv" in hits[0].content


def test_recall_no_match_empty(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("python memory")
    assert store.recall("kafka", top_k=3) == []


def test_recall_bumps_usage_count(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("uv for the environment")
    store.recall("uv")
    assert store.all()[0].usage_count == 1


def test_recall_persists_meta(tmp_path):
    store = MemoryStore(str(tmp_path))
    store.add("uv for the environment", tags=["env", "uv"], source_session="s1")
    store2 = MemoryStore(str(tmp_path))
    hits = store2.recall("uv")
    assert len(hits) == 1
    assert hits[0].source_session == "s1"
    assert hits[0].tags == ["env", "uv"]


def test_tokens_cjk_and_english():
    toks = _tokens("你好 world_1")
    assert "world_1" in toks
    assert "你" in toks and "好" in toks


def test_corrupt_lines_skipped(tmp_path):
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(os.path.join(str(tmp_path), "memories.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"id":"a","content":"ok"}\n')
        f.write("garbage-line\n")
        f.write('{"content":"bad tags","tags":"nope"}\n')
    store = MemoryStore(str(tmp_path))
    assert len(store.all()) == 2
    assert store.all()[1].tags == []
