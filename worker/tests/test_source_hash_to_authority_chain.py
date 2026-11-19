"""Producer contract: CouchDB chunk hash reaches approved PG card verbatim."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.llm_brain_miner import _chunk_source_provenance
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span
from agent_knowledge.session_memory.transcript_model import TranscriptChunk


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def test_chunk_hash_survives_producer_to_authority(isolated_pg_store):
    project = "producer-" + uuid.uuid4().hex[:10]
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "producer-" + uuid.uuid4().hex)
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=provider, project=project,
        turn_start_index=0, turn_end_index=0, text="synthetic source prose",
    )
    source_doc = dm.build_conversation_chunk_document(chunk=chunk)
    source_hash = str(source_doc["content_hash"])

    provenance = _chunk_source_provenance(source_doc, scope="project")
    assert provenance["content_hash"] == source_hash
    candidate = build_memory_card_candidate_from_source_span({
        **provenance,
        "card_type": "preference",
        "title": "synthetic producer card",
        "redacted_summary": "synthetic producer card",
        "typed_payload": {
            "preference": "synthetic", "explicitness": "explicit", "repeated_count": 1,
            "confirmation_status": "confirmed", "applies_to": "producer_contract",
        },
        "confidence": 0.9,
    }, refresh_watermark="synthetic")
    assert candidate["content_hash"] == source_hash

    steward = BrainStewardService(_NoCardLedger(), pgvector_store=isolated_pg_store,
                                  allow_restricted=True)
    memory_id = steward.candidate_create(source_span=candidate)["memory_id"]
    steward.candidate_approve(
        candidate_memory_id=memory_id, approved_by="test",
        decision_id="producer-" + uuid.uuid4().hex,
    )
    as_of = datetime.now(timezone.utc) + timedelta(seconds=60)
    rows = isolated_pg_store.list_authorized_cards(
        project=project, memory_ids=[memory_id], limit=1, steward_only=True, as_of=as_of
    )
    assert rows and rows[0]["content_hash"] == source_hash
    found = isolated_pg_store.find_authorized_by_content_hashes(
        project=project, content_hashes=[source_hash], as_of=as_of
    )
    assert found["by_hash"][source_hash] == [memory_id]
