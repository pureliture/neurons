"""Scratch-only RED/GREEN tests for safe local-embedding outbox failure metadata."""

from __future__ import annotations

import inspect
import threading
from typing import Any

import pytest

from agent_knowledge.local_embedding import (
    CanonicalLocalEmbeddingClient,
    LOCAL_MODEL2VEC_DIMENSIONS,
    LocalEmbeddingClientError,
    LocalEmbeddingServiceApplication,
    create_local_embedding_http_server,
)
from agent_knowledge.local_embedding.service import EncodedBatchResult
from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore


class _CapturingStore:
    def __init__(self, jobs: list[Any]) -> None:
        self._jobs = jobs
        self.failures: list[dict[str, Any]] = []

    def claim_local_outbox_leases(self, **_kwargs: Any) -> list[Any]:
        return self._jobs

    def mark_local_outbox_failed(self, **kwargs: Any) -> str:
        self.failures.append(kwargs)
        return "dead_letter"

    def complete_local_outbox_cas(self, **_kwargs: Any) -> bool:
        return True


def _job() -> Any:
    from agent_knowledge.postgres_store.pgvector_store import LocalOutboxJob

    return LocalOutboxJob(
        outbox_id=1,
        target_type="session_chunk",
        target_id="scratch-only",
        profile_id="lbrain-memory-model2vec-256-v1",
        source_content_hash="sha256:source",
        input_hash="sha256:input",
        payload_text="synthetic payload",
        fencing_token=1,
    )


def _run_worker(embed: Any) -> dict[str, Any]:
    store = _CapturingStore([_job()])
    worker = LocalEmbeddingOutboxWorker(store=store, embedding_fn=embed, max_retries=5)
    worker.run_once()
    assert len(store.failures) == 1
    return store.failures[0]


def test_embedding_client_error_records_allowlisted_metadata() -> None:
    failure = _run_worker(
        lambda _text: (_ for _ in ()).throw(
            LocalEmbeddingClientError(
                "internal_error",
                "internal secret /srv/private/fixture",
                status_code=500,
                retryable=False,
            )
        )
    )

    assert failure["error_code"] == (
        "terminal_error:internal_error"
        "|stage=embedding|error_class=client_error|error_param=internal_error"
    )
    assert "/Users" not in failure["error_code"]
    assert "secret" not in failure["error_code"]


def test_vector_shape_failure_records_vector_stage() -> None:
    failure = _run_worker(lambda _text: [])
    assert failure["error_code"] == (
        "terminal_error:ValueError"
        "|stage=vector_shape|error_class=value_error|error_param=ValueError"
    )


def test_unknown_client_code_is_not_echoed() -> None:
    failure = _run_worker(
        lambda _text: (_ for _ in ()).throw(
            LocalEmbeddingClientError(
                "leak:/srv/private/fixture",
                "leak:/srv/private/fixture",
                status_code=500,
                retryable=False,
            )
        )
    )
    assert "/Users" not in failure["error_code"]
    assert "leak:" not in failure["error_code"]
    assert failure["error_code"].startswith("terminal_error:unclassified|")


def test_transient_requeue_matches_code_prefix_with_instrumentation_suffix() -> None:
    source = inspect.getsource(PgVectorStore.requeue_local_dead_letters)
    assert "split_part(regexp_replace(last_error, '^retryable_error:', ''), '|', 1)" in source


class _FakeEngine:
    def check_ready(self) -> dict[str, Any]:
        return {"ready": True}

    def encode_batch(self, items: list[str]) -> EncodedBatchResult:
        vectors = [[0.1] + [0.0] * (LOCAL_MODEL2VEC_DIMENSIONS - 1) for _ in items]
        return EncodedBatchResult(vectors=vectors, prompt_tokens=sum(len(i) for i in items))


class _RaisingEngine(_FakeEngine):
    def encode_batch(self, items: list[str]) -> EncodedBatchResult:
        raise RuntimeError("renderer crash /srv/private/fixture")


@pytest.mark.parametrize("engine_factory, expected_ok", [(_FakeEngine, True), (_RaisingEngine, False)])
def test_synthetic_122881_byte_korean_input_through_local_service(
    engine_factory: Any, expected_ok: bool
) -> None:
    text = "가" * ((122881 - 1) // 3) + "a"
    assert len(text.encode("utf-8")) == 122881

    app = LocalEmbeddingServiceApplication(engine=engine_factory())
    server = create_local_embedding_http_server(
        application=app, bind_host="127.0.0.1", port=0
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = CanonicalLocalEmbeddingClient(
        base_url=f"http://{host}:{port}", use_openai_sdk=True
    )
    try:
        if expected_ok:
            result = client.embed_document(text)
            assert result.chunk_count == 16
            assert len(result.vector) == LOCAL_MODEL2VEC_DIMENSIONS
        else:
            with pytest.raises(LocalEmbeddingClientError) as exc_info:
                client.embed_document(text)
            assert exc_info.value.code == "internal_error"
            assert exc_info.value.retryable is True
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_transient_http_502_returns_retry_state_not_dead_letter() -> None:
    failure = _run_worker(
        lambda _text: (_ for _ in ()).throw(
            LocalEmbeddingClientError(
                "http_502",
                "local embedding service returned HTTP 502",
                status_code=502,
                retryable=True,
                stage="batch_http",
            )
        )
    )
    assert failure["retryable"] is True
    assert failure["error_code"].startswith("retryable_error:http_502")
