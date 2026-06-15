from __future__ import annotations

import json

from agent_knowledge.session_memory.extraction_llm import (
    build_openai_compat_completion_fn,
    build_openai_compat_embedding_fn,
)


def test_openai_compat_completion_fn_posts_chat_completions_and_returns_content():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "[{\"x\":1}]"}}]})

    fn = build_openai_compat_completion_fn(
        "http://127.0.0.1:8930/v1", "gemini-3.5-flash-thinking", post_fn=fake_post
    )
    out = fn([{"role": "user", "content": "hi"}])

    assert out == '[{"x":1}]'
    assert captured["url"] == "http://127.0.0.1:8930/v1/chat/completions"
    assert captured["body"]["model"] == "gemini-3.5-flash-thinking"
    assert captured["body"]["messages"][0]["content"] == "hi"


def test_openai_compat_completion_fn_returns_empty_on_bad_response():
    fn = build_openai_compat_completion_fn(
        "http://x/v1", "m", post_fn=lambda url, *, headers, body: "not json"
    )
    assert fn([{"role": "user", "content": "hi"}]) == ""


def test_openai_compat_embedding_fn_posts_and_returns_vector():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["url"] = url
        captured["body"] = json.loads(body)
        return json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    fn = build_openai_compat_embedding_fn("http://x/v1", "gemini-embedding-001", post_fn=fake_post)
    vec = fn("hello")

    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://x/v1/embeddings"
    assert captured["body"]["input"] == "hello"


def test_openai_compat_no_auth_header_without_key():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["headers"] = headers
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    build_openai_compat_completion_fn("http://x/v1", "m", post_fn=fake_post)([{"role": "user", "content": "hi"}])
    assert "Authorization" not in captured["headers"]
