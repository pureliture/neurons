"""M3 verification tests: Fork manifest integrity, quality gates (>=50 queries, >=20 entity pairs),
paid fallback blocking, and fail-closed cutover readiness.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Sequence

import pytest

import graphiti_core
from agent_knowledge.llm_brain_core.bulk_semantic import OpenAICompatibleEmbeddingBatcher
from agent_knowledge.local_embedding import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
    LOCAL_MODEL2VEC_REVISION,
    LocalEmbeddingCutoverReport,
    LocalModel2VecEngine,
    Neo4jBackfillProgress,
    PgBackfillProgress,
    build_sanitized_entity_dedup_pairs,
    build_sanitized_eval_corpus,
    build_sanitized_eval_queries,
    evaluate_cutover_readiness,
    run_sanitized_quality_evaluation,
)
from agent_knowledge.model_connectors.policy import (
    PaidEmbeddingFallbackBlockedError,
    validate_local_embedding_write_or_query_policy,
)

CACHED_MODEL_DIR_ENV = os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR", "").strip()
CACHED_MODEL_DIR = Path(CACHED_MODEL_DIR_ENV) if CACHED_MODEL_DIR_ENV else None


def test_vendored_graphiti_fork_manifest_integrity() -> None:
    """Verify graphiti_core is loaded from worker/lib/graphiti_core and FORK_MANIFEST.json hashes match."""
    fork_dir = Path(graphiti_core.__file__).resolve().parent
    expected_root = Path(__file__).resolve().parents[1] / "lib" / "graphiti_core"
    assert fork_dir == expected_root, f"Expected vendored graphiti_core at {expected_root}, got {fork_dir}"

    manifest_path = fork_dir / "FORK_MANIFEST.json"
    notice_path = fork_dir / "NOTICE"
    assert manifest_path.is_file()
    assert notice_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["package"] == "graphiti-core"
    assert manifest["upstream_version"] == "0.30.1"
    assert manifest["license"] == "Apache-2.0"
    assert manifest["local_profile_id"] == LOCAL_EMBEDDING_PROFILE_ID

    for rel_path, meta in manifest["added_files"].items():
        target = fork_dir / rel_path
        assert target.is_file(), f"Missing added fork file: {rel_path}"
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual_sha == meta["fork_sha256"], f"Hash mismatch for added file {rel_path}"

    for rel_path, meta in manifest["modified_files"].items():
        target = fork_dir / rel_path
        assert target.is_file(), f"Missing modified fork file: {rel_path}"
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual_sha == meta["fork_sha256"], f"Hash mismatch for modified file {rel_path}"
        assert meta["fork_sha256"] != meta["upstream_sha256"], (
            f"Modified file {rel_path} has identical fork and upstream hashes"
        )


def test_sanitized_eval_dataset_minimum_bounds() -> None:
    """Ensure the benchmark dataset satisfies >=50 queries, >=20 entity pairs, and multi-chunk >512-token docs."""
    corpus = build_sanitized_eval_corpus()
    queries = build_sanitized_eval_queries()
    entity_pairs = build_sanitized_entity_dedup_pairs()

    assert len(corpus) >= 44
    assert len(queries) >= 50
    assert len(entity_pairs) >= 20

    non_merge_pairs = [p for p in entity_pairs if not p.should_merge]
    merge_pairs = [p for p in entity_pairs if p.should_merge]
    assert len(non_merge_pairs) >= 20
    assert len(merge_pairs) >= 4

    # Verify long documents exceed 8,192 UTF-8 bytes (>512 tokens, multi-chunk) and place tail markers in chunk 2
    by_id = {c.memory_id: c for c in corpus}
    for mem_id, tail_marker in (
        ("mem_eval_36", "ZETA-ROLLBACK-99"),
        ("mem_eval_37", "FINAL-EPOCH-SYNC-4242"),
        ("mem_eval_38", "TAIL-RECEIPT-VERIFIED-3003"),
    ):
        card = by_id[mem_id]
        raw_bytes = card.summary.encode("utf-8")
        assert len(raw_bytes) > 9000, f"{mem_id} must exceed 8,192 UTF-8 bytes"
        marker_pos = card.summary.find(tail_marker)
        assert marker_pos > 0
        prefix_bytes = card.summary[:marker_pos].encode("utf-8")
        assert len(prefix_bytes) > 8192, f"Tail marker {tail_marker} in {mem_id} must reside in chunk >= 2 (>8192B)"


@pytest.mark.skipif(
    not CACHED_MODEL_DIR or not CACHED_MODEL_DIR.is_dir(),
    reason="Real Model2Vec snapshot directory not present on this host (set NEURON_LOCAL_EMBEDDING_MODEL_DIR)",
)
def test_real_model2vec_quality_gates_54_queries_24_entity_pairs() -> None:
    """Verify real Model2Vec weights satisfy Recall@5 >= 0.90, 0 false entity merges, and 0 scope/temporal leaks."""
    engine = LocalModel2VecEngine.from_model_dir(CACHED_MODEL_DIR)

    def _embed(chunks: Sequence[str]) -> Sequence[Sequence[float]]:
        return engine.encode_batch(list(chunks)).vectors

    report = run_sanitized_quality_evaluation(
        raw_chunk_embedder=_embed,
        evaluation_mode="real_model2vec_weights",
        recall_at_5_threshold=0.90,
    )
    assert report.profile_id == LOCAL_EMBEDDING_PROFILE_ID
    assert report.model_alias == LOCAL_EMBEDDING_MODEL_ALIAS
    assert report.model_revision == LOCAL_MODEL2VEC_REVISION
    assert report.total_queries >= 50
    assert report.entity_pairs_evaluated >= 20
    assert report.recall_at_5 >= 0.90, f"Expected Recall@5 >= 0.90, got {report.recall_at_5} (missed={report.missed_query_ids})"
    assert report.entity_false_merges == 0
    assert report.entity_false_splits == 0
    assert report.auth_scope_violations == 0
    assert report.temporal_state_misclassifications == 0
    assert report.passed_proposed_gates is True


def test_paid_embedding_fallback_blocked_across_connectors_and_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local profile never resolves to a paid Gemini/OpenAI endpoint, even if paid env vars are present."""
    for key in (
        "LLM_BRAIN_EMBEDDING_BASE_URL",
        "NEURON_LOCAL_EMBEDDING_BASE_URL",
        "LLM_BRAIN_EMBEDDING_PROVIDER",
        "EMBEDDING_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    paid_env = {
        "NEURON_EMBEDDING_PROFILE": LOCAL_EMBEDDING_PROFILE_ID,
        "LLM_BRAIN_EMBEDDING_MODEL": LOCAL_EMBEDDING_MODEL_ALIAS,
        "OPENAI_API_KEY": "sk-should-never-be-used",
        "GEMINI_API_KEY": "AIza-should-never-be-used",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
    }

    # 1) policy: missing base_url / paid hosts / paid models / wrong dim are all rejected
    for kwargs in (
        {"model": LOCAL_EMBEDDING_MODEL_ALIAS, "base_url": None},
        {"model": LOCAL_EMBEDDING_MODEL_ALIAS, "base_url": "https://api.openai.com/v1"},
        {"model": LOCAL_EMBEDDING_MODEL_ALIAS, "base_url": "https://generativelanguage.googleapis.com/v1beta"},
        {"model": "gemini-embedding-2", "base_url": "http://127.0.0.1:8765/v1"},
        {"model": "text-embedding-3-large", "base_url": "http://127.0.0.1:8765/v1"},
    ):
        with pytest.raises(PaidEmbeddingFallbackBlockedError):
            validate_local_embedding_write_or_query_policy(
                profile_id=LOCAL_EMBEDDING_PROFILE_ID, dimension=LOCAL_EMBEDDING_DIM, **kwargs
            )
    with pytest.raises(PaidEmbeddingFallbackBlockedError):
        validate_local_embedding_write_or_query_policy(
            profile_id=LOCAL_EMBEDDING_PROFILE_ID,
            model=LOCAL_EMBEDDING_MODEL_ALIAS,
            base_url="http://127.0.0.1:8765/v1",
            dimension=3072,
        )

    # 2) bulk semantic: local profile without a local URL must NOT fall back to OPENAI_BASE_URL
    with pytest.raises(PaidEmbeddingFallbackBlockedError):
        OpenAICompatibleEmbeddingBatcher.from_env(paid_env)
    with pytest.raises(PaidEmbeddingFallbackBlockedError):
        OpenAICompatibleEmbeddingBatcher(
            base_url="https://api.openai.com/v1",
            model=LOCAL_EMBEDDING_MODEL_ALIAS,
            embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        )

    # 3) PG/MCP recall provider: local model + paid base url is rejected at construction
    from agent_knowledge.rag_ingress.qdrant_embedding import build_openai_embedding_provider

    with pytest.raises(Exception) as excinfo:
        build_openai_embedding_provider(
            environ={
                **paid_env,
                "LLM_BRAIN_EMBEDDING_PROVIDER": "model2vec",
                "LLM_BRAIN_EMBEDDING_BASE_URL": "https://api.openai.com/v1",
            }
        )
    assert "sk-should-never-be-used" not in str(excinfo.value)


def test_local_service_down_never_contacts_paid_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the local service unreachable, only the configured local endpoint is ever dialed."""
    import socket

    from agent_knowledge.local_embedding.client import CanonicalLocalEmbeddingClient

    dialed: list[tuple[str, int]] = []
    real_create_connection = socket.create_connection
    real_connect = socket.socket.connect

    def _record_create(address, *args, **kwargs):
        dialed.append((str(address[0]), int(address[1])))
        return real_create_connection(address, *args, **kwargs)

    def _record_connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            dialed.append((str(address[0]), int(address[1])))
        return real_connect(self, address)

    monkeypatch.setattr(socket, "create_connection", _record_create)
    monkeypatch.setattr(socket.socket, "connect", _record_connect)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-used")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-should-never-be-used")

    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:59999/v1",
        api_key="local-model2vec",
        timeout_seconds=2.0,
        use_openai_sdk=True,
    )
    try:
        with pytest.raises(Exception):
            client.embed_document("로컬 임베딩 서비스 장애 시 유료 호출이 없어야 한다")
    finally:
        client.close()

    assert dialed, "expected at least one connection attempt to the local endpoint"
    assert {host for host, _ in dialed} <= {"127.0.0.1", "localhost", "::1"}
    assert {port for _, port in dialed} == {59999}


def test_cutover_readiness_fail_closed_gate() -> None:
    """evaluate_cutover_readiness returns ready=True only when all PG and Neo4j backlog/quarantine counts are 0."""
    clean_pg = PgBackfillProgress(
        eligible_cards=10,
        ready_cards=10,
        missing_cards=0,
        eligible_sessions=25,
        ready_sessions=25,
        missing_sessions=0,
        eligible_session_receipts=25,
        ready_session_receipts=25,
        missing_session_receipts=0,
        session_receipts_verified=True,
        outbox_queued=0,
        outbox_processing=0,
        outbox_failed_retryable=0,
        outbox_dead_letter=0,
        quarantined=0,
    )
    clean_neo4j = Neo4jBackfillProgress(
        group_ids_scanned=("neurons",),
        eligible_entities=15,
        ready_entities=15,
        missing_or_dirty_entities=0,
        eligible_edges=20,
        ready_edges=20,
        missing_or_dirty_edges=0,
        groups_fenced_busy=0,
        groups_quarantined=0,
    )

    ready_report: LocalEmbeddingCutoverReport = evaluate_cutover_readiness(
        pg_progress=clean_pg,
        neo4j_progress=clean_neo4j,
    )
    assert ready_report.ready is True
    assert ready_report.blocking_reasons == ()

    dirty_pg = PgBackfillProgress(
        eligible_cards=10,
        ready_cards=9,
        missing_cards=1,
        outbox_dead_letter=2,
    )
    dirty_neo4j = Neo4jBackfillProgress(
        missing_or_dirty_edges=3,
        groups_quarantined=1,
    )
    blocked_report = evaluate_cutover_readiness(
        pg_progress=dirty_pg,
        neo4j_progress=dirty_neo4j,
    )
    assert blocked_report.ready is False

    unverified_receipts_pg = PgBackfillProgress(
        eligible_cards=10,
        ready_cards=10,
        eligible_sessions=25,
        ready_sessions=25,
        session_receipts_verified=False,
    )
    unverified_report = evaluate_cutover_readiness(
        pg_progress=unverified_receipts_pg,
        neo4j_progress=clean_neo4j,
    )
    assert unverified_report.ready is False
    assert (
        "pg_session_receipts_unverified_for_couchdb_authority"
        in unverified_report.blocking_reasons
    )

    # In direct migrated reader mode, unverified couchdb receipts do not block cutover
    direct_report = evaluate_cutover_readiness(
        pg_progress=unverified_receipts_pg,
        neo4j_progress=clean_neo4j,
        direct_migrated_reader=True,
    )
    assert direct_report.ready is True
    assert "pg_missing_cards=1" in blocked_report.blocking_reasons
    assert "pg_outbox_dead_letter=2" in blocked_report.blocking_reasons
    assert "neo4j_missing_or_dirty_edges=3" in blocked_report.blocking_reasons
    assert "neo4j_groups_quarantined=1" in blocked_report.blocking_reasons
