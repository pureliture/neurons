from __future__ import annotations

import json

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.rag_ingress.server_runtime import public_ingress_leak_violations
from agent_knowledge.session_memory.transcript_model import (
    REDACTION_VERSION,
    TranscriptChunk,
    TranscriptSession,
    _sha256,
)


def _session(**overrides) -> TranscriptSession:
    base = dict(
        session_id_hash=dm.build_session_id_hash("codex", "sess-001"),
        provider="codex",
        project="/Users/dev/Projects/neurons",
        started_at="2026-06-17T01:00:00Z",
        ended_at="2026-06-17T01:30:00Z",
        source_status="source_proven",
        source_locator_hash=dm.build_source_locator_hash("/private/spool/codex/sess-001.jsonl"),
    )
    base.update(overrides)
    return TranscriptSession(**base)


def _chunk(text: str = "user asked about the migration plan", part_index: int = 1) -> TranscriptChunk:
    return TranscriptChunk.from_text(
        chunk_id=f"conv_{part_index:03d}",
        session_id_hash=dm.build_session_id_hash("codex", "sess-001"),
        provider="codex",
        project="neurons",
        turn_start_index=0,
        turn_end_index=4,
        text=text,
    )


# --- hashing / id determinism -------------------------------------------------


def test_sha256_hash_parity_with_transcript_pipeline() -> None:
    assert dm.sha256_hash("codex:sess-001") == _sha256("codex:sess-001")
    assert dm.build_session_id_hash("codex", "sess-001") == _sha256("codex:sess-001")


def test_doc_ids_are_deterministic_and_partitioned() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    assert dm.session_doc_id(sid) == dm.session_doc_id(sid)
    assert dm.conversation_chunk_doc_id(sid, "chunk_aa") != dm.conversation_chunk_doc_id(sid, "chunk_bb")
    assert dm.conversation_chunk_doc_id(sid, "chunk_aa").startswith("conversation_chunk:")
    # _id carries the hex digest, never the "sha256:" prefix or raw value.
    assert "sha256:" not in dm.session_doc_id(sid)


def test_coverage_hash_is_order_independent() -> None:
    a = dm.build_coverage_hash(["sha256:aa", "sha256:bb", "sha256:cc"])
    b = dm.build_coverage_hash(["sha256:cc", "sha256:aa", "sha256:bb"])
    assert a == b
    assert a != dm.build_coverage_hash(["sha256:aa", "sha256:bb"])


# --- document builders --------------------------------------------------------


def test_transcript_session_document_carries_owner_schema_and_canonical_project() -> None:
    doc = dm.build_transcript_session_document(session=_session())
    assert doc["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert doc["owner"] == dm.COUCHDB_SOURCE_OWNER
    assert doc["schema_version"] == dm.COUCHDB_SOURCE_SCHEMA_VERSION
    # path-form project canonicalized to the repo label
    assert doc["project"] == "neurons"
    assert doc["_id"] == dm.session_doc_id(doc["session_id_hash"])


def test_conversation_chunk_document_has_content_hash_and_body() -> None:
    chunk = _chunk()
    doc = dm.build_conversation_chunk_document(chunk=chunk)
    assert doc["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK
    assert doc["content_hash"] == chunk.content_hash
    assert doc["body"] == chunk.redacted_text
    assert doc["redaction_version"] == REDACTION_VERSION
    assert doc["_id"] == dm.conversation_chunk_doc_id(chunk.session_id_hash, chunk.chunk_id)


def test_tool_evidence_bundle_records_index_range_and_coverage_hash() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    member_hashes = ["sha256:" + "1" * 64, "sha256:" + "2" * 64]
    doc = dm.build_tool_evidence_bundle_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=2,
        evidence_index_start=0,
        evidence_index_end=4,
        record_content_hashes=member_hashes,
        body="test_result pass: 12 passed",
    )
    assert doc["doc_type"] == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
    assert doc["evidence_index_start"] == 0
    assert doc["evidence_index_end"] == 4
    assert doc["evidence_count"] == 2
    assert doc["coverage_hash"] == dm.build_coverage_hash(member_hashes)
    assert doc["content_hash"] == dm.sha256_hash("test_result pass: 12 passed")


def test_tool_evidence_bundle_rejects_inverted_index_range() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    with pytest.raises(ValueError):
        dm.build_tool_evidence_bundle_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            part_index=1,
            part_count=1,
            evidence_index_start=5,
            evidence_index_end=1,
            record_content_hashes=[],
            body="x",
        )


def test_coverage_manifest_holds_counts_and_coverage_hashes() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    doc = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        conversation_chunk_count=3,
        tool_evidence_bundle_count=1,
        conversation_content_hashes=["sha256:aa", "sha256:bb"],
        tool_evidence_coverage_hashes=["sha256:cc"],
        ledger_comparison={"index_candidate_count": 2, "ledger_turn_count": 4},
    )
    assert doc["conversation_chunk_count"] == 3
    assert doc["tool_evidence_bundle_count"] == 1
    assert doc["ledger_comparison"]["index_candidate_count"] == 2


# --- ownership rules ----------------------------------------------------------


def test_assert_couchdb_owned_rejects_unknown_and_retired() -> None:
    for owned in dm.COUCHDB_OWNED_DOC_TYPES:
        dm.assert_couchdb_owned(owned)  # no raise
    with pytest.raises(dm.OwnershipViolation):
        dm.assert_couchdb_owned("transcript-memory")
    with pytest.raises(dm.OwnershipViolation):
        dm.assert_couchdb_owned("session-memory")


def test_projection_state_rejects_transcript_memory_target() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    ok = dm.build_projection_state_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        projection_status=dm.ProjectionStatus.PROJECTED,
        session_memory_knowledge_id="sha256:" + "9" * 64,
        active_content_hash="sha256:" + "9" * 64,
    )
    assert ok["target_profile"] == dm.RETIRED_INDEX_BRIDGE_RECALL_PROFILE
    with pytest.raises(dm.OwnershipViolation):
        dm.build_projection_state_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            projection_status=dm.ProjectionStatus.PROJECTED,
            target_profile=dm.RETIRED_RETIRED_INDEX_BRIDGE_PROFILE,
        )


def test_retention_manifest_tier_rules() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    for tier in (dm.RetentionTier.HOT_FULL, dm.RetentionTier.HOT_MANIFEST_ONLY):
        doc = dm.build_retention_manifest_document(
            session_id_hash=sid, provider="codex", project="neurons", tier=tier
        )
        assert doc["tier"] == tier
    cold = dm.build_retention_manifest_document(
        session_id_hash=sid,
        provider="codex",
        project="neurons",
        tier=dm.RetentionTier.COLD_ARCHIVE_REF,
        cold_archive_ref="archive://2026-06/codex/sess-001",
    )
    assert cold["cold_archive_ref"].startswith("archive://")
    with pytest.raises(ValueError):
        dm.build_retention_manifest_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            tier=dm.RetentionTier.COLD_ARCHIVE_REF,
        )
    with pytest.raises(ValueError):
        dm.build_retention_manifest_document(
            session_id_hash=sid, provider="codex", project="neurons", tier="bogus"
        )


# --- redaction / public-safety boundary --------------------------------------


def test_build_rejects_body_with_local_path_leak() -> None:
    sid = dm.build_session_id_hash("codex", "sess-001")
    leaking_body = "ran job at " + "/Users/" + "exampleuser/work/notes.md"
    # sanity: the synthetic body really does trip the shared leak gate
    assert public_ingress_leak_violations(leaking_body)
    with pytest.raises(dm.SourceRedactionLeak) as exc:
        dm.build_tool_evidence_bundle_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            part_index=1,
            part_count=1,
            evidence_index_start=0,
            evidence_index_end=0,
            record_content_hashes=["sha256:aa"],
            body=leaking_body,
        )
    # the exception is public-safe: it names categories, not the raw path
    assert "exampleuser" not in str(exc.value)


def test_build_rejects_secret_like_metadata_via_chunk_path() -> None:
    # session/chunk fields are fixed, so secret-like keys can only enter through
    # the coverage manifest's ledger_comparison block; assert it is screened.
    sid = dm.build_session_id_hash("codex", "sess-001")
    with pytest.raises(dm.SecretLikeMetadataError):
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            conversation_chunk_count=0,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[],
            tool_evidence_coverage_hashes=[],
            ledger_comparison={"api_key": "should-never-pass"},
        )


def test_no_raw_path_or_id_in_serialized_documents() -> None:
    docs = [
        dm.build_transcript_session_document(session=_session()),
        dm.build_conversation_chunk_document(chunk=_chunk()),
    ]
    for doc in docs:
        blob = json.dumps(doc, ensure_ascii=False)
        assert public_ingress_leak_violations(blob) == []
        assert "/Users/" not in blob
        assert "/private/" not in blob


def test_session_id_hash_must_be_hash_not_raw() -> None:
    with pytest.raises(ValueError):
        dm.build_transcript_session_document(session=_session(session_id_hash="codex:sess-001"))
