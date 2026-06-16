from __future__ import annotations

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.retention import (
    RetentionInput,
    RetentionPolicy,
    apply_retention,
    plan_retention,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.session_memory.transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptChunk,
    TranscriptSession,
)

POLICY = RetentionPolicy(hot_full_max_age_days=30, hot_manifest_max_age_days=90)


def _sid() -> str:
    return dm.build_session_id_hash("codex", "sess-1")


def _seed(store):
    sid = _sid()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid, provider="codex", project="neurons", started_at="2026-06-17T01:00:00Z"
            )
        )
    )
    for i, text in enumerate(("a", "b")):
        store.put(
            dm.build_conversation_chunk_document(
                chunk=TranscriptChunk.from_text(
                    chunk_id=f"chunk_{i}",
                    session_id_hash=sid,
                    provider="codex",
                    project="neurons",
                    turn_start_index=i,
                    turn_end_index=i,
                    text=text,
                )
            )
        )
    store_tool_evidence_bundles(
        [
            ToolEvidenceSummaryRecord(
                session_id_hash=sid,
                provider="codex",
                project="neurons",
                category="test_result",
                outcome="pass",
                tool_name="bash",
                command_summary="x",
                redacted_summary="12 passed",
                evidence_index=0,
            )
        ],
        store=store,
    )
    return sid


# --- planning -----------------------------------------------------------------


def test_recent_source_stays_hot_full():
    d = plan_retention(RetentionInput(session_id_hash=_sid(), age_days=5), policy=POLICY)
    assert d.desired_tier == dm.RetentionTier.HOT_FULL
    assert d.effective_tier == dm.RetentionTier.HOT_FULL
    assert d.allowed is True


def test_old_source_needs_projection_coverage_and_backup():
    # past hot_full age but missing all gates -> blocked, stays hot_full
    d = plan_retention(RetentionInput(session_id_hash=_sid(), age_days=60), policy=POLICY)
    assert d.desired_tier == dm.RetentionTier.HOT_MANIFEST_ONLY
    assert d.allowed is False
    assert d.effective_tier == dm.RetentionTier.HOT_FULL
    assert set(d.blocking) == {
        "session_memory_not_projected",
        "cold_archive_ref_missing",
    }


def test_manifest_tier_allowed_when_gates_met():
    d = plan_retention(
        RetentionInput(
            session_id_hash=_sid(),
            age_days=60,
            projection_status=dm.ProjectionStatus.PROJECTED,
            coverage_intact=True,
            cold_archive_ref="archive://2026-04/codex/s1",
        ),
        policy=POLICY,
    )
    assert d.effective_tier == dm.RetentionTier.HOT_MANIFEST_ONLY
    assert d.allowed is True


def test_very_old_source_goes_cold_with_backup():
    d = plan_retention(
        RetentionInput(
            session_id_hash=_sid(),
            age_days=400,
            projection_status=dm.ProjectionStatus.PROJECTED,
            cold_archive_ref="archive://2025/codex/s1",
        ),
        policy=POLICY,
    )
    assert d.desired_tier == dm.RetentionTier.COLD_ARCHIVE_REF
    assert d.allowed is True


# --- applying -----------------------------------------------------------------


def test_dry_run_does_not_mutate():
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store)
    before = len(store.all_docs())
    d = plan_retention(
        RetentionInput(
            session_id_hash=sid,
            age_days=60,
            projection_status=dm.ProjectionStatus.PROJECTED,
            cold_archive_ref="archive://x",
        ),
        policy=POLICY,
    )
    result = apply_retention(decision=d, store=store, dry_run=True)
    assert result["dry_run"] is True
    assert result["compacted"] is False
    assert len(result["deleted_doc_ids"]) == 3  # 2 chunks + 1 bundle
    assert len(store.all_docs()) == before  # nothing removed


def test_apply_compacts_bodies_but_keeps_manifests():
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store)
    # add a coverage manifest so it survives compaction
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider="codex",
            project="neurons",
            conversation_chunk_count=2,
            tool_evidence_bundle_count=1,
            conversation_content_hashes=["sha256:" + "a" * 64],
            tool_evidence_coverage_hashes=["sha256:" + "b" * 64],
        )
    )
    d = plan_retention(
        RetentionInput(
            session_id_hash=sid,
            age_days=120,
            projection_status=dm.ProjectionStatus.PROJECTED,
            cold_archive_ref="archive://2025/codex/s1",
        ),
        policy=POLICY,
    )
    result = apply_retention(decision=d, store=store, dry_run=False)
    assert result["compacted"] is True
    # heavy bodies gone from hot store
    assert store.find_by_session(session_id_hash=sid, doc_type=dm.SourceDocType.CONVERSATION_CHUNK) == []
    assert store.find_by_session(session_id_hash=sid, doc_type=dm.SourceDocType.TOOL_EVIDENCE_BUNDLE) == []
    # manifests survive for audit/rollback
    assert store.find_by_session(session_id_hash=sid, doc_type=dm.SourceDocType.COVERAGE_MANIFEST)
    retention = store.get(dm.retention_manifest_doc_id(sid))
    assert retention["tier"] == dm.RetentionTier.COLD_ARCHIVE_REF
    assert retention["cold_archive_ref"] == "archive://2025/codex/s1"


def test_blocked_decision_does_not_delete_even_when_applied():
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store)
    before = len(store.all_docs())
    d = plan_retention(RetentionInput(session_id_hash=sid, age_days=120), policy=POLICY)  # no gates met
    result = apply_retention(decision=d, store=store, dry_run=False)
    assert result["compacted"] is False
    # only the retention manifest (tier=hot_full) is added; nothing deleted
    assert store.find_by_session(session_id_hash=sid, doc_type=dm.SourceDocType.CONVERSATION_CHUNK)
    retention = store.get(dm.retention_manifest_doc_id(sid))
    assert retention["tier"] == dm.RetentionTier.HOT_FULL
    assert len(store.all_docs()) == before + 1
