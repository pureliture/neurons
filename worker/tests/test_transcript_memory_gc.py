import hashlib
import json

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory.transcript_memory_gc import (
    CANDIDATE_SCOPE_SESSION_SEARCH,
    TranscriptMemoryGcConfig,
    TranscriptMemoryGcRunner,
    main,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk


PROJECT = "workspace-ragflow-advisor"
SESSION_ID_HASH = "sha256:transcript-memory-gc-target"
TRANSCRIPT_DATASET_ID = "ds_transcript_memory"
SESSION_DATASET_ID = "ds_session_memory"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return _sha(material)


def _source_window_hash(
    *,
    content_hash: str,
    turn_start_index: int,
    turn_end_index: int,
    redaction_version: str,
) -> str:
    material = "|".join(
        [
            "session_memory_source_window.v1",
            content_hash,
            str(turn_start_index),
            str(turn_end_index),
            redaction_version,
        ]
    )
    return _sha(material)


def _conversation_chunk(
    ledger: Ledger,
    *,
    knowledge_id: str = "kn_transcript_gc_source",
    document_id: str = "doc_transcript_gc_source",
    session_id_hash: str = SESSION_ID_HASH,
) -> dict:
    chunk = TranscriptChunk.from_text(
        chunk_id=f"chunk_{knowledge_id}",
        session_id_hash=session_id_hash,
        provider="codex",
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        text="user: transcript memory source covered by session memory.",
        source_status="indexed_transcript_memory",
    )
    row = ledger.upsert_transcript_chunk(knowledge_id=knowledge_id, chunk=chunk)
    ledger.mark_uploaded(row["knowledge_id"], dataset_id=TRANSCRIPT_DATASET_ID, document_id=document_id, run="DONE")
    ledger.mark_indexed(row["knowledge_id"], run="DONE")
    return Ledger(ledger.path).get_by_knowledge_id(row["knowledge_id"])


def _active_session_memory(ledger: Ledger, *, source: dict, match_window: bool = True) -> dict:
    source_window_hash = _source_window_hash(
        content_hash=source["content_hash"],
        turn_start_index=1 if match_window else 2,
        turn_end_index=1 if match_window else 2,
        redaction_version="redaction.v2",
    )
    source_pairs = [(source["content_hash"], source_window_hash)]
    item = ledger.upsert_session_memory(
        knowledge_id="kn_transcript_gc_replacement",
        content_hash=_sha("session-memory-replacement"),
        provider="codex",
        project=PROJECT,
        session_id_hash=SESSION_ID_HASH,
        title="replacement",
        summary="replacement",
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_edge_manifest_hash(source_pairs),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source["content_hash"],
        source_window_hash=source_window_hash,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id=SESSION_DATASET_ID, document_id="doc_session_memory_active", run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    ledger.promote_session_memory(item["knowledge_id"])
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=item["knowledge_id"],
    )
    return Ledger(ledger.path).get_by_knowledge_id(item["knowledge_id"])


def test_transcript_memory_gc_dry_run_lists_covered_source_after_active_replacement(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    source = _conversation_chunk(ledger)
    _active_session_memory(ledger, source=source)

    report = TranscriptMemoryGcRunner(
        config=TranscriptMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=TRANSCRIPT_DATASET_ID,
            session_memory_dataset_id=SESSION_DATASET_ID,
            ragflow_url="http://localhost:9380",
            min_indexed_age_seconds=0,
            execute_disable=False,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 1
    assert report["disable_selected_count"] == 1
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["hard_delete_performed"] is False


def test_transcript_memory_gc_execute_disable_is_blocked_in_worker_slice(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    source = _conversation_chunk(ledger)
    _active_session_memory(ledger, source=source)

    report = TranscriptMemoryGcRunner(
        config=TranscriptMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=TRANSCRIPT_DATASET_ID,
            session_memory_dataset_id=SESSION_DATASET_ID,
            ragflow_url="http://localhost:9380",
            min_indexed_age_seconds=0,
            execute_disable=True,
        ),
        token="test-token",
    ).run()

    assert report["status"] == "blocked_live_execution"
    assert report["eligible_count"] == 1
    assert report["disabled_count"] == 0
    assert Ledger(ledger_path).get_by_knowledge_id(source["knowledge_id"])["status"] == "indexed"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_transcript_memory_gc_blocks_without_matching_coverage(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    source = _conversation_chunk(ledger)
    _active_session_memory(ledger, source=source, match_window=False)
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="still-dirty",
        source_knowledge_id=source["knowledge_id"],
    )

    report = TranscriptMemoryGcRunner(
        config=TranscriptMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id=TRANSCRIPT_DATASET_ID,
            session_memory_dataset_id=SESSION_DATASET_ID,
            ragflow_url="http://localhost:9380",
            min_indexed_age_seconds=0,
            execute_disable=False,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 0
    assert report["disable_selected_count"] == 0


def test_transcript_memory_gc_cli_dry_run_reports_json_without_network(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--dataset-id",
        TRANSCRIPT_DATASET_ID,
        "--session-memory-dataset-id",
        SESSION_DATASET_ID,
        "--ragflow-url",
        "http://localhost:9380",
    ])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_transcript_memory_gc_cli_execute_disable_is_fail_closed(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--dataset-id",
        TRANSCRIPT_DATASET_ID,
        "--session-memory-dataset-id",
        SESSION_DATASET_ID,
        "--ragflow-url",
        "http://localhost:9380",
        "--execute-disable",
    ])

    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_transcript_memory_gc_cli_requires_search_surface_verification(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--dataset-id",
        TRANSCRIPT_DATASET_ID,
        "--candidate-scope",
        CANDIDATE_SCOPE_SESSION_SEARCH,
        "--ragflow-url",
        "http://localhost:9380",
    ])

    assert exit_code == 2
    assert "requires --verify-search-surface" in capsys.readouterr().err
