from __future__ import annotations

import json
import traceback

import pytest

from agent_knowledge.session_memory.extraction_llm import (
    ExtractionResponseError,
    ExtractionTransportError,
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
        "http://127.0.0.1:8930/v1", "gemma-4-26b-a4b-it-maas", post_fn=fake_post
    )
    out = fn([{"role": "user", "content": "hi"}])

    assert out == '[{"x":1}]'
    assert captured["url"] == "http://127.0.0.1:8930/v1/chat/completions"
    assert captured["body"]["model"] == "gemma-4-26b-a4b-it-maas"
    assert captured["body"]["messages"][0]["content"] == "hi"


@pytest.mark.parametrize("raw", [
    "not json",
    "null",
    "{}",
    '{"choices": []}',
    '{"choices": null}',
    '{"choices": [{}]}',
    '{"choices": [{"message": null}]}',
    '{"choices": [{"message": {"content": null}}]}',
    '{"choices": [{"message": {"content": ""}}]}',
    '{"choices": [{"message": {"content": "   "}}]}',
    '{"choices": [{"message": {"content": 42}}]}',
    '{"choices": [{"message": {"content": []}}]}',
    '{"choices": [{"message": {"content": {"sensitive-marker": "private"}}}]}',
])
def test_openai_compat_completion_fn_propagates_typed_bad_response(raw):
    fn = build_openai_compat_completion_fn(
        "http://synthetic.invalid/v1", "m", post_fn=lambda url, *, headers, body: raw
    )
    with pytest.raises(RuntimeError) as error:
        fn([{"role": "user", "content": "hi"}])
    assert type(error.value).__name__ == "ExtractionResponseError"
    assert "sensitive-marker" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("error_type", [TimeoutError, OSError, RuntimeError])
def test_openai_compat_completion_fn_propagates_redacted_transport_failure(error_type):
    def failed_post(url, *, headers, body):
        raise error_type("sensitive-marker https://private.invalid/api?key=private")

    fn = build_openai_compat_completion_fn(
        "http://synthetic.invalid/v1", "m", api_key="synthetic-key", post_fn=failed_post
    )
    with pytest.raises(RuntimeError) as error:
        fn([{"role": "user", "content": "hi"}])
    assert type(error.value).__name__ == "ExtractionTransportError"
    serialized = str(error.value) + repr(error.value) + "".join(traceback.format_exception(error.value))
    assert "sensitive-marker" not in serialized
    assert "private.invalid" not in serialized
    assert "synthetic-key" not in serialized


def test_openai_compat_completion_fn_keeps_successful_explicit_empty_json_content():
    fn = build_openai_compat_completion_fn(
        "http://synthetic.invalid/v1", "m",
        post_fn=lambda url, *, headers, body: json.dumps({"choices": [{"message": {"content": "[]"}}]}),
    )
    assert fn([{"role": "user", "content": "hi"}]) == "[]"


@pytest.mark.parametrize("miner_kind", ["batch", "envelope", "legacy"])
@pytest.mark.parametrize("failure", ["timeout", "transport", "response"])
def test_real_completion_adapter_failures_reach_miners_instead_of_empty_results(miner_kind, failure):
    from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner
    from agent_knowledge.session_memory.llm_brain_miner import LlmBrainEnvelopeMiner
    from agent_knowledge.session_memory.memory_miner import LlmMemoryMiner

    def post(url, *, headers, body):
        if failure == "timeout":
            raise TimeoutError("synthetic-marker")
        if failure == "transport":
            raise OSError("synthetic-marker")
        return '{"choices": []}'

    completion = build_openai_compat_completion_fn("http://synthetic.invalid/v1", "synthetic", post_fn=post)
    chunk = {"redacted_text": "Keep the approved boundary.", "project": "neurons", "provider": "codex",
             "knowledge_id": "synthetic-source", "content_hash": "sha256:" + "a" * 64}
    expected_error = ExtractionResponseError if failure == "response" else ExtractionTransportError
    with pytest.raises(expected_error):
        if miner_kind == "batch":
            BatchedEnvelopeMiner(completion_fn=completion).mine_batches([chunk], refresh_watermark="wm")
        elif miner_kind == "envelope":
            LlmBrainEnvelopeMiner(completion_fn=completion).mine_chunk(chunk, refresh_watermark="wm")
        else:
            LlmMemoryMiner(completion_fn=completion).mine_chunk(chunk)


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


def test_openai_compat_extra_headers_are_merged():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["headers"] = headers
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    fn = build_openai_compat_completion_fn(
        "http://x/v1", "m", api_key="k", post_fn=fake_post,
        extra_headers={"x-opencode-session": "s1"},
    )
    assert fn([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["headers"]["x-opencode-session"] == "s1"


def test_openai_compat_sets_sdk_style_user_agent():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["headers"] = headers
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    build_openai_compat_completion_fn("http://x/v1", "m", post_fn=fake_post)([{"role": "user", "content": "hi"}])
    assert captured["headers"]["User-Agent"].startswith("OpenAI/Python")


def test_intake_completion_env_builds_opencode_target_with_session_header():
    from agent_knowledge.session_memory.extraction_llm import build_intake_completion_from_env

    captured = {}

    def fake_post(url, *, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    env = {
        "LLM_BRAIN_EXTRACTION_BASE_URL": "https://opencode.ai/zen/go/v1",
        "LLM_BRAIN_EXTRACTION_MODEL": "mimo-v2.6-flash",
        "LLM_BRAIN_EXTRACTION_API_KEY": "zk",
    }
    fn = build_intake_completion_from_env(env, post_fn=fake_post)
    assert fn([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.6-flash"
    assert captured["headers"]["Authorization"] == "Bearer zk"
    assert captured["headers"]["x-opencode-session"] == "neurons-memory-intake"


def test_openai_compat_completion_fn_adds_session_header_for_opencode_endpoint():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["headers"] = headers
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    fn = build_openai_compat_completion_fn(
        "https://opencode.ai/zen/go/v1", "mimo-v2.6-flash", post_fn=fake_post
    )
    assert fn([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["headers"]["x-opencode-session"]


def test_openai_compat_completion_fn_omits_session_header_for_other_providers():
    captured = {}

    def fake_post(url, *, headers, body):
        captured["headers"] = headers
        return json.dumps({"choices": [{"message": {"content": "ok"}}]})

    fn = build_openai_compat_completion_fn(
        "http://model-gateway-proxy:8930/v1", "m", post_fn=fake_post
    )
    assert fn([{"role": "user", "content": "hi"}]) == "ok"
    assert "x-opencode-session" not in captured["headers"]


def test_intake_completion_env_falls_back_to_vertex_wrapper(monkeypatch):
    import agent_knowledge.session_memory.extraction_llm as module

    def sentinel(_messages):
        return "sentinel"

    monkeypatch.setattr(module, "build_vertex_wrapper_completion_fn", lambda **kwargs: sentinel)
    fn = module.build_intake_completion_from_env({})
    assert fn([{"role": "user", "content": "hi"}]) == "sentinel"
