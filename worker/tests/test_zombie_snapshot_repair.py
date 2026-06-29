from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory.zombie_snapshot_repair import (
    ZOMBIE_SNAPSHOT_REPAIR_MARKER,
    ZombieSnapshotRepairConfig,
    ZombieSnapshotRepairRunner,
)


PROJECT = "workspace-index-advisor"
SESSION_ID_HASH = "sha256:session-memory-zombie-target"


def _sha(label: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    import hashlib

    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_memory(ledger: Ledger, *, knowledge_id: str, session_id_hash: str = SESSION_ID_HASH) -> dict:
    source_content_hash = _sha(f"{knowledge_id}:source")
    source_window_hash = _sha(f"{knowledge_id}:window")
    item = ledger.upsert_session_memory(
        knowledge_id=knowledge_id,
        content_hash=_sha(knowledge_id),
        provider="codex",
        project=PROJECT,
        session_id_hash=session_id_hash,
        title=knowledge_id,
        summary=knowledge_id,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_edge_manifest_hash([(source_content_hash, source_window_hash)]),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source_content_hash,
        source_window_hash=source_window_hash,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds_session_memory", document_id=f"doc_{knowledge_id}", run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    return ledger.get_by_knowledge_id(item["knowledge_id"])


def _runner(ledger_path, *, execute: bool):
    return ZombieSnapshotRepairRunner(
        config=ZombieSnapshotRepairConfig(
            ledger_path=ledger_path,
            max_items=100,
            execute=execute,
        )
    )


def test_zombie_snapshot_repair_requeues_promoted_dirty_with_disabled_active_snapshot(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    item = _session_memory(ledger, knowledge_id="kn_zombie")
    ledger.promote_session_memory(item["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="bulk_session_memory_backfill_from_indexed_transcript_memory_2026_05_21",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=item["knowledge_id"],
    )
    ledger.mark_disabled(item["knowledge_id"])

    dry = _runner(ledger_path, execute=False).run()
    executed = _runner(ledger_path, execute=True).run()
    repeated = _runner(ledger_path, execute=True).run()

    assert dry["eligible_count"] == 1
    assert dry["mutation_performed"] is False
    assert executed["requeued_count"] == 1
    assert executed["removed_snapshot_count"] == 1
    assert Ledger(ledger_path).get_session_memory_active_snapshot(SESSION_ID_HASH) is None
    dirty = Ledger(ledger_path).get_dirty_session_memory(SESSION_ID_HASH)
    assert dirty["status"] == "pending"
    assert dirty["reason"] == "bulk_session_memory_backfill_from_indexed_transcript_memory_2026_05_21"
    assert dirty["last_error_class"] == ZOMBIE_SNAPSHOT_REPAIR_MARKER
    assert repeated["eligible_count"] == 0
    assert repeated["requeued_count"] == 0


def test_zombie_snapshot_repair_leaves_valid_active_snapshot_alone(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    item = _session_memory(ledger, knowledge_id="kn_valid")
    ledger.promote_session_memory(item["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="new_chunk_indexed",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=item["knowledge_id"],
    )

    report = _runner(ledger_path, execute=True).run()

    assert report["eligible_count"] == 0
    assert Ledger(ledger_path).get_session_memory_active_snapshot(SESSION_ID_HASH)["active_knowledge_id"] == item["knowledge_id"]
    assert Ledger(ledger_path).get_dirty_session_memory(SESSION_ID_HASH)["status"] == "promoted"


def test_zombie_snapshot_repair_cleans_pending_dirty_snapshot_without_changing_reason(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    item = _session_memory(ledger, knowledge_id="kn_pending_zombie")
    ledger.promote_session_memory(item["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="new_chunk_indexed",
    )
    ledger.mark_disabled(item["knowledge_id"])

    report = _runner(ledger_path, execute=True).run()

    assert report["eligible_count"] == 1
    assert report["requeued_count"] == 1
    assert Ledger(ledger_path).get_session_memory_active_snapshot(SESSION_ID_HASH) is None
    dirty = Ledger(ledger_path).get_dirty_session_memory(SESSION_ID_HASH)
    assert dirty["status"] == "pending"
    assert dirty["reason"] == "new_chunk_indexed"
