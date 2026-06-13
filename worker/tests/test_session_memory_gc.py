import hashlib
import json
from datetime import datetime, timedelta, timezone

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory.session_memory_gc import (
    MIN_DISABLED_AGE_FLOOR_SECONDS,
    SessionMemoryGcConfig,
    SessionMemoryGcRunner,
    main,
)


PROJECT = "workspace-ragflow-advisor"
SESSION_ID_HASH = "sha256:session-memory-gc-target"
DATASET_ID = "ds_session_memory"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return _sha(material)


def _backdate_disabled_at(ledger: Ledger, knowledge_id: str, *, seconds_ago: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    with ledger._connect() as connection:
        connection.execute(
            "UPDATE knowledge_items SET disabled_at = ? WHERE knowledge_id = ?",
            (stamp, knowledge_id),
        )


def _session_memory(ledger: Ledger, *, knowledge_id: str, document_id: str, session_id_hash: str = SESSION_ID_HASH):
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
    ledger.mark_uploaded(item["knowledge_id"], dataset_id=DATASET_ID, document_id=document_id, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    return ledger.get_by_knowledge_id(item["knowledge_id"])


def test_session_memory_gc_dry_run_lists_disabled_row_after_replacement_active(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old", document_id="doc_gc_old")
    active = _session_memory(ledger, knowledge_id="kn_gc_active", document_id="doc_gc_active")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=DATASET_ID,
            ragflow_url="http://localhost:9380",
            execute=False,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 1
    assert report["selected_count"] == 1
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_session_memory_gc_execute_is_blocked_in_worker_slice(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old", document_id="doc_gc_old")
    active = _session_memory(ledger, knowledge_id="kn_gc_active", document_id="doc_gc_active")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(session_id_hash=SESSION_ID_HASH, provider="codex", project=PROJECT, reason="gc-test")
    ledger.mark_dirty_session_memory_promoted(session_id_hash=SESSION_ID_HASH, summary_knowledge_id=active["knowledge_id"])

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=DATASET_ID,
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert report["status"] == "blocked_live_execution"
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 0
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_session_memory_gc_floors_min_disabled_age_to_block_fresh_disable(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_fresh", document_id="doc_gc_old_fresh")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_fresh", document_id="doc_gc_active_fresh")
    ledger.mark_disabled(old["knowledge_id"])
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(session_id_hash=SESSION_ID_HASH, provider="codex", project=PROJECT, reason="gc-test")
    ledger.mark_dirty_session_memory_promoted(session_id_hash=SESSION_ID_HASH, summary_knowledge_id=active["knowledge_id"])

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=DATASET_ID,
            ragflow_url="http://localhost:9380",
            min_disabled_age_seconds=0,
            execute=False,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 0
    assert report["min_disabled_age_floor_seconds"] == MIN_DISABLED_AGE_FLOOR_SECONDS
    assert report["effective_min_disabled_age_seconds"] == MIN_DISABLED_AGE_FLOOR_SECONDS


def test_session_memory_gc_cli_dry_run_reports_json_without_network(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--dataset-id",
        DATASET_ID,
        "--ragflow-url",
        "http://localhost:9380",
    ])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_session_memory_gc_cli_execute_is_fail_closed(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--dataset-id",
        DATASET_ID,
        "--ragflow-url",
        "http://localhost:9380",
        "--execute",
    ])

    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
