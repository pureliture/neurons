"""Contract: CLI claims each chunk once; writer token equals claimed token."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_full_input_pg_state as pg_state
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_semantic import DeterministicGraphitiSemanticWriter
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROJECT = "claim-contract"
POLICY = "full-input.v1"


class _FakeExtractor:
    model = "free-model"
    provider = "synthetic-provider"
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def __init__(self) -> None:
        self.calls = []

    def extract(self, batch):  # type: ignore[no-untyped-def]
        self.calls.append([item.text for item in batch])
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                             type="Concept", summary="synthetic"),),
                relations=(),
            ) for item in batch
        ))


class _Writer:
    def __init__(self) -> None:
        self.tokens = []

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):  # type: ignore[no-untyped-def]
        for item in inputs:
            self.tokens.append(int(item.episode.payload.get("fence_token") or 0))
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            projected_natural_ids=tuple(i.episode.natural_id for i in inputs),
            chunk_receipts={item.session_key: "receipt:" + str(item.session_key)
                            for item in inputs},
        )


class _State:
    def __init__(self) -> None:
        self.claims: list[str] = []
        self.counter = 0

    def claim(self, key: str, owner: str) -> int:
        self.claims.append(key)
        self.counter += 1
        return self.counter

    def is_complete(self, key: str) -> bool:
        return False

    def register_plan(self, **kwargs) -> None:
        return None

    def is_session_complete(self, **kwargs) -> bool:
        return False

    def complete_many(self, entries) -> None:
        return None

    def complete(self, key: str, **kwargs) -> None:
        return None

    def record_chunk(self, **kwargs) -> None:
        return None


def _seed(store) -> str:
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, "claim-session")
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=PROJECT,
        started_at="2026-06-21T00:00:00Z",
    )))
    hashes = []
    for index, body in enumerate(["FRONT" + "A" * 32, "MIDDLE" + "B" * 32, "C" * 32 + "END"]):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index}", session_id_hash=sid, provider=provider,
            project=PROJECT, turn_start_index=index, turn_end_index=index, text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(doc["content_hash"])
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=provider, project=PROJECT,
        conversation_chunk_count=3, tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": PROJECT, "ambiguous": False,
                           "eligible_for_retirement": True},
    ))
    return sid


def test_cli_claims_each_chunk_once_and_writer_uses_claimed_token(monkeypatch, tmp_path, capsys):
    store = InMemoryCouchDBSourceStore()
    sid = _seed(store)
    extractor = _FakeExtractor()
    writer = _Writer()
    state = _State()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
                        classmethod(lambda cls, **kwargs: extractor))
    monkeypatch.setattr(DeterministicGraphitiSemanticWriter, "from_env",
                        classmethod(lambda cls, **kwargs: writer))
    monkeypatch.setattr(cli, "PgChunkState", lambda dsn: state)
    monkeypatch.setattr(pg_state, "PgChunkState", lambda dsn: state)
    monkeypatch.setattr(cli, "_pg_authority_by_hash", lambda dsn, project, hashes: {})
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_PG_DSN", "postgresql://synthetic@127.0.0.1:1/synthetic")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    rc = cli.main([
        "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
        "--project", PROJECT, "--limit", "1",
        "--full-input-policy-version", POLICY,
    ])
    captured = capsys.readouterr()
    report = json.loads(captured.out or captured.err or "{}")
    assert len(state.claims) == 3, state.claims
    assert len(set(state.claims)) == 3, state.claims
    assert sorted(writer.tokens) == [1, 2, 3], writer.tokens
    assert rc == 0, json.dumps(report, sort_keys=True)[:4000]
    assert sid
