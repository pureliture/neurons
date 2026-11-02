"""M1 tests for the local Model2Vec service, `canonical_embedding_v1`, and OpenAI/Graphiti SDK compatibility."""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from openai import APIStatusError, AsyncOpenAI, BadRequestError, OpenAI, RateLimitError

from agent_knowledge.local_embedding import (
    CanonicalEmbeddingAggregationError,
    CanonicalEmbeddingInputError,
    CanonicalLocalEmbeddingClient,
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_FILE_SHA256,
    LOCAL_MODEL2VEC_LIBRARY_VERSION,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
    LOCAL_MODEL2VEC_PROFILE_ID,
    LOCAL_MODEL2VEC_REVISION,
    LocalEmbeddingClientError,
    LocalEmbeddingModelIntegrityError,
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    LocalModel2VecGraphitiEmbedder,
    aggregate_chunk_vectors_v1,
    card_input_v1,
    compute_input_hash,
    create_local_embedding_http_server,
    decode_base64_float32_le,
    encode_base64_float32_le,
    install_verified_model,
    plan_request_batches_v1,
    prepare_canonical_input_v1,
    session_input_v1,
    split_utf8_chunks_v1,
    verify_model_directory,
)
from agent_knowledge.local_embedding import cli as local_embedding_cli


def _discover_real_model_dir() -> Path | None:
    env_dir = os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR", "").strip()
    if not env_dir:
        return None
    candidate = Path(env_dir)
    if candidate.is_dir() and all((candidate / name).is_file() for name in LOCAL_MODEL2VEC_FILE_SHA256):
        return candidate
    return None


@pytest.fixture(scope="module")
def real_model_dir() -> Path:
    found = _discover_real_model_dir()
    if found is None:
        pytest.skip("Pinned local Model2Vec snapshot directory not present on this host")
    return found


@pytest.fixture(scope="module")
def real_engine(real_model_dir: Path) -> LocalModel2VecEngine:
    return LocalModel2VecEngine.from_model_dir(real_model_dir)


@pytest.fixture()
def running_real_server(real_engine: LocalModel2VecEngine):
    app = LocalEmbeddingServiceApplication(engine=real_engine)
    server = create_local_embedding_http_server(
        application=app,
        bind_host="127.0.0.1",
        port=0,
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=raw_body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            payload = json.loads(resp.read().decode("utf-8"))
            return status, resp_headers, payload
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        payload = json.loads(exc.read().decode("utf-8"))
        return exc.code, resp_headers, payload


def test_profile_verification_and_install_from_local_snapshot(real_model_dir: Path, tmp_path: Path) -> None:
    verified = verify_model_directory(real_model_dir)
    assert verified["profile_id"] == LOCAL_MODEL2VEC_PROFILE_ID
    assert verified["model"] == LOCAL_MODEL2VEC_MODEL_ALIAS
    assert verified["revision"] == LOCAL_MODEL2VEC_REVISION
    assert verified["library_version"] == LOCAL_MODEL2VEC_LIBRARY_VERSION
    assert verified["dimensions"] == LOCAL_MODEL2VEC_DIMENSIONS
    assert verified["files"] == dict(LOCAL_MODEL2VEC_FILE_SHA256)

    target = tmp_path / "installed_model"
    install_res = install_verified_model(target, source_dir=real_model_dir, allow_download=False)
    assert install_res["status"] == "verified"
    assert (target / "INSTALL_MANIFEST.json").is_file()

    # Tamper with config.json and ensure verify_model_directory fails closed without leaking target path.
    (target / "config.json").write_bytes(b'{"tampered": true}')
    with pytest.raises(LocalEmbeddingModelIntegrityError) as exc_info:
        verify_model_directory(target)
    assert exc_info.value.reason_code == "sha256_mismatch:config.json"
    assert str(target) not in exc_info.value.public_message


def test_cli_verify_and_profile_commands(real_model_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc_profile = local_embedding_cli.main(["profile"])
    assert rc_profile == 0
    out_profile = json.loads(capsys.readouterr().out)
    assert out_profile["profile_id"] == LOCAL_MODEL2VEC_PROFILE_ID

    rc_verify = local_embedding_cli.main(["verify", "--model-dir", str(real_model_dir), "--skip-load-probe"])
    assert rc_verify == 0
    out_verify = json.loads(capsys.readouterr().out)
    assert out_verify["status"] == "verified"


def test_canonical_utf8_chunking_preserves_multibyte_boundaries() -> None:
    # Korean 3-byte UTF-8 characters + 4-byte emoji around 8192-byte boundaries.
    unit = "가나다라마바사아자차카타파하🧠"
    text = unit * 600  # ~27,000 UTF-8 bytes
    raw = text.encode("utf-8")
    assert len(raw) > 8192 * 3

    chunks = split_utf8_chunks_v1(text, max_chunk_bytes=8192)
    assert len(chunks) >= 4
    for chunk in chunks[:-1]:
        chunk_bytes = len(chunk.encode("utf-8"))
        # Must be within [8192 - 3, 8192] bytes for valid UTF-8 prefix.
        assert 8189 <= chunk_bytes <= 8192
    assert "".join(chunks) == text
    assert b"".join(c.encode("utf-8") for c in chunks) == raw

    prep = prepare_canonical_input_v1(text, max_chunk_bytes=8192)
    assert prep.input_hash == compute_input_hash(text)
    assert prep.total_utf8_bytes == len(raw)
    assert prep.chunks == chunks


def test_canonical_vector_aggregation_and_request_batch_planning() -> None:
    v1 = [1.0] + [0.0] * 255
    v2 = [0.0, 1.0] + [0.0] * 254
    agg = aggregate_chunk_vectors_v1([v1, v2], [6000, 2000])
    assert len(agg) == 256
    norm = math.sqrt(sum(x * x for x in agg))
    assert math.isclose(norm, 1.0, rel_tol=1e-6)
    # 6000:2000 = 3:1 ratio -> [0.94868..., 0.31622..., 0...]
    assert math.isclose(agg[0] / agg[1], 3.0, rel_tol=1e-6)

    with pytest.raises(CanonicalEmbeddingAggregationError):
        aggregate_chunk_vectors_v1([[0.0] * 256], [100])

    # Batch planning: 20 chunks of 8000 bytes each -> must split by both 16-item and 128KiB limits.
    chunks = [("가" * 2600) + f"-{i}" for i in range(20)]
    batches = plan_request_batches_v1(chunks)
    assert len(batches) == 2
    assert sum(len(b) for b in batches) == 20
    assert [item for b in batches for item in b] == chunks
    for b in batches:
        assert len(b) <= 16
        assert sum(len(c.encode("utf-8")) for c in b) <= 131072


def test_card_and_session_input_helpers() -> None:
    assert card_input_v1(summary="핵심 요약", title="제목", typed_payload={"a": 1}) == "핵심 요약"
    assert card_input_v1(summary="   ", title="대체 제목", typed_payload={"a": 1}) == "대체 제목"
    assert (
        card_input_v1(summary="", title=None, typed_payload={"b": "값", "a": 1})
        == '{"a":1,"b":"값"}'
    )
    with pytest.raises(CanonicalEmbeddingInputError):
        card_input_v1(summary="", title="", typed_payload={})
    with pytest.raises(CanonicalEmbeddingInputError):
        session_input_v1("   ")


def test_livez_readyz_and_unavailable_state() -> None:
    unready_app = LocalEmbeddingServiceApplication(engine=None, startup_error_code="model_dir_missing")
    live_resp = unready_app.handle_livez()
    assert live_resp.status_code == 200
    live_body = json.loads(live_resp.body.decode("utf-8"))
    assert live_body["live"] is True
    assert live_body["ready"] is False

    ready_resp = unready_app.handle_readyz()
    assert ready_resp.status_code == 503
    assert ready_resp.headers.get("Retry-After") == "2"
    ready_body = json.loads(ready_resp.body.decode("utf-8"))
    assert ready_body["ready"] is False
    assert ready_body["error"]["code"] == "model_unavailable"

    emb_resp = unready_app.handle_embeddings(
        json.dumps({"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "테스트"}).encode("utf-8")
    )
    assert emb_resp.status_code == 503
    assert emb_resp.headers.get("Retry-After") == "2"
    emb_body = json.loads(emb_resp.body.decode("utf-8"))
    assert emb_body["error"]["code"] == "model_unavailable"


def test_server_strict_validation_and_error_envelopes(running_real_server: str) -> None:
    endpoint = f"{running_real_server}/v1/embeddings"

    # 1. Duplicate JSON keys -> 400 invalid_request
    status, _, body = _http_request(
        endpoint,
        method="POST",
        raw_body=b'{"model":"local-model2vec-256-v1","model":"local-model2vec-256-v1","input":"hello"}',
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"

    # 2. Unknown JSON key -> 400 invalid_request
    status, _, body = _http_request(
        endpoint,
        method="POST",
        raw_body=json.dumps(
            {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "hello", "extra_field": 1}
        ).encode("utf-8"),
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"

    # 3. Unknown model -> 400 unknown_model
    status, _, body = _http_request(
        endpoint,
        method="POST",
        raw_body=json.dumps({"model": "gemini-embedding-2", "input": "hello"}).encode("utf-8"),
    )
    assert status == 400
    assert body["error"]["code"] == "unknown_model"

    # 4. Invalid dimensions (boolean True, 3072, null) -> 400 invalid_request
    for invalid_dim in (True, 3072, None, "256"):
        status, _, body = _http_request(
            endpoint,
            method="POST",
            raw_body=json.dumps(
                {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "hello", "dimensions": invalid_dim}
            ).encode("utf-8"),
        )
        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    # 5. Invalid encoding_format -> 400 invalid_request
    for invalid_fmt in (None, "hex", 123):
        status, _, body = _http_request(
            endpoint,
            method="POST",
            raw_body=json.dumps(
                {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "hello", "encoding_format": invalid_fmt}
            ).encode("utf-8"),
        )
        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    # 6. Invalid input types (empty string, empty list, list with empty string, token IDs) -> 400 invalid_request
    for invalid_input in ("", [], ["valid", ""], [1, 2, 3], [[1, 2]], None, 42):
        status, _, body = _http_request(
            endpoint,
            method="POST",
            raw_body=json.dumps(
                {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": invalid_input}
            ).encode("utf-8"),
        )
        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    # 7. Item > 8192 UTF-8 bytes -> 413 input_too_large
    status, _, body = _http_request(
        endpoint,
        method="POST",
        raw_body=json.dumps(
            {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "가" * 3000}
        ).encode("utf-8"),
    )
    assert status == 413
    assert body["error"]["code"] == "input_too_large"

    # 8. Batch > 16 items -> 413 input_too_large
    status, _, body = _http_request(
        endpoint,
        method="POST",
        raw_body=json.dumps(
            {"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": [f"item-{i}" for i in range(17)]}
        ).encode("utf-8"),
    )
    assert status == 413
    assert body["error"]["code"] == "input_too_large"


def test_real_openai_sdk_and_graphiti_embedder_float_and_base64(running_real_server: str) -> None:
    sdk_client = OpenAI(
        api_key="local-test-key",
        base_url=f"{running_real_server}/v1",
        max_retries=0,
    )

    texts = [
        "Neurons 서버는 로컬 Model2Vec 256차원 임베딩을 사용한다.",
        "Graphiti와 PostgreSQL pgvector가 동일한 프로필을 공유한다.",
        "OpenAI SDK base64 wire format compatibility verification.",
    ]

    # Default OpenAI SDK call sends encoding_format="base64" over the wire and decodes to floats.
    resp_default = sdk_client.embeddings.create(
        model=LOCAL_MODEL2VEC_MODEL_ALIAS,
        input=texts,
        user="test-caller-not-logged",
    )
    assert resp_default.model == LOCAL_MODEL2VEC_MODEL_ALIAS
    assert len(resp_default.data) == 3
    assert [item.index for item in resp_default.data] == [0, 1, 2]
    assert resp_default.usage.prompt_tokens > 0
    assert resp_default.usage.total_tokens == resp_default.usage.prompt_tokens

    # Explicit float format via raw HTTP
    status_f, _, body_f = _http_request(
        f"{running_real_server}/v1/embeddings",
        method="POST",
        raw_body=json.dumps(
            {
                "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                "input": texts,
                "encoding_format": "float",
                "dimensions": 256,
            }
        ).encode("utf-8"),
    )
    assert status_f == 200

    # Explicit base64 format via raw HTTP
    status_b, _, body_b = _http_request(
        f"{running_real_server}/v1/embeddings",
        method="POST",
        raw_body=json.dumps(
            {
                "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                "input": texts,
                "encoding_format": "base64",
                "dimensions": 256,
            }
        ).encode("utf-8"),
    )
    assert status_b == 200

    for idx in range(3):
        vec_sdk = resp_default.data[idx].embedding
        vec_float = body_f["data"][idx]["embedding"]
        vec_b64 = decode_base64_float32_le(body_b["data"][idx]["embedding"])
        assert len(vec_sdk) == 256
        assert len(vec_float) == 256
        assert len(vec_b64) == 256
        for a, b, c in zip(vec_sdk, vec_float, vec_b64):
            assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
            assert math.isclose(a, c, rel_tol=1e-6, abs_tol=1e-6)

    # Verify Graphiti OpenAIEmbedder (`create` and `create_batch`) + LocalModel2VecGraphitiEmbedder
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    graphiti_sdk_embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key="local-test-key",
            embedding_model=LOCAL_MODEL2VEC_MODEL_ALIAS,
            embedding_dim=LOCAL_MODEL2VEC_DIMENSIONS,
            base_url=f"{running_real_server}/v1",
        )
    )
    single_vec = asyncio.run(graphiti_sdk_embedder.create(texts[0]))
    batch_vecs = asyncio.run(graphiti_sdk_embedder.create_batch(texts))
    assert len(single_vec) == 256
    assert len(batch_vecs) == 3
    for a, b in zip(single_vec, batch_vecs[0]):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def test_korean_over_512_tokens_under_8kib_and_long_multi_chunk_preservation(
    running_real_server: str,
) -> None:
    client = CanonicalLocalEmbeddingClient(
        base_url=running_real_server,
        use_openai_sdk=True,
    )

    # 1. Under 8KiB UTF-8 (~5.5 KiB) but > 512 tokens in Korean:
    # Verify prompt_tokens > 512 and tail change after 512 tokens alters the vector!
    prefix_sentences = [
        f"제{i}조: 로컬 임베딩 서비스는 유료 외부 호출 없이 안전하게 문서를 처리한다."
        for i in range(1, 75)
    ]
    base_prefix = " ".join(prefix_sentences)
    doc_tail_alpha = base_prefix + " 마지막 결론: 포스트그레스 큐 펜싱과 캐스 검증을 통과하였다."
    doc_tail_beta = base_prefix + " 마지막 결론: 네오포제이 그래프 삭제 툼스톤과 저널 복구를 완료하였다."

    assert len(doc_tail_alpha.encode("utf-8")) < 8192
    assert len(doc_tail_beta.encode("utf-8")) < 8192

    res_alpha = client.embed_document(doc_tail_alpha)
    res_beta = client.embed_document(doc_tail_beta)

    assert res_alpha.chunk_count == 1
    assert res_beta.chunk_count == 1
    assert res_alpha.prompt_tokens > 512
    assert res_beta.prompt_tokens > 512
    assert res_alpha.input_hash != res_beta.input_hash
    # Vectors must differ because max_length=None prevents 512-token truncation!
    cosine_sim = sum(a * b for a, b in zip(res_alpha.vector, res_beta.vector))
    assert cosine_sim < 0.999999
    assert any(not math.isclose(a, b, abs_tol=1e-5) for a, b in zip(res_alpha.vector, res_beta.vector))

    # 2. Multi-chunk (> 8192 bytes, ~22 KiB across 3 chunks):
    # Verify changes in beginning (chunk 0), middle (chunk 1), and end (chunk 2) are all reflected!
    filler_chunk = ("공통 배경 설명 문장입니다. 시스템은 결정론적 UTF-8 바이트 분할을 수행합니다. " * 70)
    assert 7000 < len(filler_chunk.encode("utf-8")) < 8000

    long_base = filler_chunk + filler_chunk + filler_chunk
    long_head_mod = "핵심키워드_머리부분_알파벳_변경 " + filler_chunk + filler_chunk + filler_chunk
    long_mid_mod = filler_chunk + " 핵심키워드_중간부분_베타_변경 " + filler_chunk + filler_chunk
    long_tail_mod = filler_chunk + filler_chunk + filler_chunk + " 핵심키워드_꼬리부분_감마_변경"

    r_base = client.embed_document(long_base)
    r_head = client.embed_document(long_head_mod)
    r_mid = client.embed_document(long_mid_mod)
    r_tail = client.embed_document(long_tail_mod)

    assert r_base.chunk_count >= 3
    assert r_head.chunk_count >= 3
    assert r_mid.chunk_count >= 3
    assert r_tail.chunk_count >= 3

    for other in (r_head, r_mid, r_tail):
        assert other.input_hash != r_base.input_hash
        assert any(not math.isclose(a, b, abs_tol=1e-5) for a, b in zip(other.vector, r_base.vector))


def test_concurrency_queue_limit_and_timeout_429() -> None:
    started_event = threading.Event()
    release_event = threading.Event()

    class _SlowEngine:
        def check_ready(self) -> dict[str, Any]:
            return {"ready": True}

        def encode_batch(self, items: list[str]):
            started_event.set()
            release_event.wait(timeout=5.0)
            from agent_knowledge.local_embedding.service import EncodedBatchResult

            vec = [1.0 / math.sqrt(256.0)] * 256
            return EncodedBatchResult(vectors=[vec for _ in items], prompt_tokens=len(items))

    # Concurrency=1, max_queue_size=2, queue_timeout_seconds=0.3
    app = LocalEmbeddingServiceApplication(
        engine=_SlowEngine(),  # type: ignore[arg-type]
        max_concurrency=1,
        max_queue_size=2,
        queue_timeout_seconds=0.3,
    )
    req_body = json.dumps({"model": LOCAL_MODEL2VEC_MODEL_ALIAS, "input": "test"}).encode("utf-8")

    results: list[int] = []

    def _worker() -> None:
        resp = app.handle_embeddings(req_body)
        results.append(resp.status_code)

    t1 = threading.Thread(target=_worker)
    t1.start()
    assert started_event.wait(timeout=2.0)

    # Start 2 queued requests (fills queue of size 2) + 1 overflow request (immediate 429)
    t2 = threading.Thread(target=_worker)
    t3 = threading.Thread(target=_worker)
    t2.start()
    t3.start()
    time.sleep(0.05)

    overflow_resp = app.handle_embeddings(req_body)
    assert overflow_resp.status_code == 429
    assert overflow_resp.headers.get("Retry-After") == "2"
    overflow_payload = json.loads(overflow_resp.body.decode("utf-8"))
    assert overflow_payload["error"]["code"] == "capacity_exceeded"

    # Wait for t2 and t3 to time out in queue (0.3s), then release t1
    time.sleep(0.4)
    release_event.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    t3.join(timeout=2.0)

    assert sorted(results) == [200, 429, 429]
