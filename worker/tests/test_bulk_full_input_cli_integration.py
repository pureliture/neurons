"""Integration TDD: drive the real product bulk CLI with synthetic sources.

No network, no production DB/K8s, no real data. Fake model and isolated writer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input import (
    FullInputState,
    build_session_grounding_fact,
    enforce_free_only_extractor,
    validate_source_documents,
)
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
    DeterministicGraphitiSemanticWriter,
    OpenAICompatibleBulkSemanticExtractor,
)
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROJECT = "synthetic-project"
PROVIDER = "synthetic-provider"
POLICY = "full-input.v1"


class _FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.model = "free-model"
        self.provider = "synthetic-provider"
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):  # type: ignore[no-untyped-def]
        self.calls.append([item.text for item in batch])
        return BulkSemanticExtractionResult(
            tuple(
                BulkSemanticSessionResult(
                    session_key=item.session_key,
                    entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                                 type="Concept", summary="synthetic"),),
                    relations=(),
                )
                for item in batch
            )
        )


class _PaidExtractor(_FakeExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.model = "paid-model"
        self.price_status = "paid"


class _ShadowWriter:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):  # type: ignore[no-untyped-def]
        by_key = extraction.by_session_key()
        for item in inputs:
            result = by_key[item.session_key]
            self.writes.append(
                {
                    "session_key": item.session_key,
                    "group": str(item.episode.payload.get("graph_group_id") or ""),
                    "entity_count": len(result.entities),
                    "text_digest": str(item.episode.payload.get("canonical_chunk_hash") or ""),
                }
            )
        return BulkSemanticWriteReport(
            projected=len(inputs),
            entities_written=sum(len(by_key[i.session_key].entities) for i in inputs),
            relations_written=0,
            projected_natural_ids=tuple(i.episode.natural_id for i in inputs),
        )


def _seed(store: InMemoryCouchDBSourceStore, bodies: list[str]) -> str:
    sid = dm.build_session_id_hash(PROVIDER, "synthetic-session")
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(session_id_hash=sid, provider=PROVIDER,
                                      project=PROJECT, started_at="2026-06-21T00:00:00Z")
        )
    )
    hashes: list[str] = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index}", session_id_hash=sid, provider=PROVIDER,
            project=PROJECT, turn_start_index=index, turn_end_index=index,
            text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid, provider=PROVIDER, project=PROJECT,
            conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
            conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
            project_authority={"project": PROJECT, "ambiguous": False,
                               "eligible_for_retirement": True},
        )
    )
    return sid


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.delenv("TRIAL_PG_DSN", raising=False)
    monkeypatch.delenv("TRIAL_NEO4J_URI", raising=False)
    monkeypatch.delenv("TRIAL_NEO4J_PASSWORD", raising=False)
    store = InMemoryCouchDBSourceStore()
    extractor = _FakeExtractor()
    writer = _ShadowWriter()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(
        OpenAICompatibleBulkSemanticExtractor, "from_env", classmethod(lambda cls, **kwargs: extractor)
    )
    monkeypatch.setattr(
        DeterministicGraphitiSemanticWriter, "from_env", classmethod(lambda cls, **kwargs: writer)
    )
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    args = [
        "--ledger", str(tmp_path / "ledger.db"),
        "--runtime-dir", str(tmp_path / "runtime"),
        "--project", PROJECT,
        "--limit", "1",
        "--full-input-policy-version", POLICY,
    ]
    return SimpleNamespace(store=store, extractor=extractor, writer=writer, args=args,
                           ledger=tmp_path / "ledger.db", runtime=tmp_path / "runtime")


def _run(env, capsys):
    rc = cli.main(list(env.args))
    captured = capsys.readouterr()
    report = json.loads(captured.out or captured.err)
    return rc, report


def test_real_cli_full_input_chunk_to_shadow_writer_and_restart_dedupe(cli_env, capsys):
    _seed(cli_env.store, [
        "FRONT" + "A" * 4000,
        "MIDDLE" + "B" * 4000,
        "C" * 4000 + "END",
    ])
    rc, report = _run(cli_env, capsys)
    assert rc == 0, json.dumps(report, sort_keys=True)
    assert report["status"] == "ok"
    assert report["full_input_policy_version"] == POLICY
    assert report["full_input_chunks"] == 3
    assert len(cli_env.extractor.calls) == 1
    texts = cli_env.extractor.calls[0]
    assert len(texts) == 3
    assert "FRONT" in texts[0] and "MIDDLE" in texts[1] and "END" in texts[2]
    assert all("[...]" not in text for text in texts)
    assert len(cli_env.writer.writes) == 3
    assert all(write["group"].startswith("shadow_") for write in cli_env.writer.writes)
    assert len({write["text_digest"] for write in cli_env.writer.writes}) == 3

    state = FullInputState(cli_env.runtime)
    assert len(state.completed_chunks(PROJECT, POLICY)) == 3

    calls_before = len(cli_env.extractor.calls)
    rc2, report2 = _run(cli_env, capsys)
    assert rc2 == 0
    assert report2["projection"]["skipped_resumed"] >= 1, (report2, state._data)
    assert len(cli_env.extractor.calls) == calls_before
    assert len(cli_env.writer.writes) == 3


def test_real_cli_reprocesses_after_source_hash_change(cli_env, capsys):
    sid = _seed(cli_env.store, ["front", "middle", "end"])
    _run(cli_env, capsys)
    before = len(cli_env.extractor.calls)
    docs = cli_env.store.find_by_session(session_id_hash=sid)
    chunk = next(doc for doc in docs if doc.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)
    changed = TranscriptChunk.from_text(
        chunk_id="chunk_0", session_id_hash=sid, provider=PROVIDER, project=PROJECT,
        turn_start_index=0, turn_end_index=0, text="front CHANGED",
    )
    changed_doc = dm.build_conversation_chunk_document(chunk=changed)
    cli_env.store.put({**changed_doc, "_rev": chunk.get("_rev")})
    rc, report = _run(cli_env, capsys)
    assert rc == 0
    assert report["full_input_chunks"] == 3
    assert len(cli_env.extractor.calls) > before


def test_real_cli_quarantines_missing_source_segment_without_model_call(cli_env, capsys):
    sid = _seed(cli_env.store, ["front", "middle", "end"])
    docs = cli_env.store.find_by_session(session_id_hash=sid)
    chunk = next(doc for doc in docs if doc.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)
    cli_env.store.delete(str(chunk["_id"]))
    rc, report = _run(cli_env, capsys)
    assert rc == 1
    assert report["status"] in {"partial", "failed"}
    assert report["full_input_source_invalid"] >= 1
    assert cli_env.extractor.calls == []


def test_real_cli_fails_closed_on_paid_extractor_before_any_call(cli_env, capsys):
    _seed(cli_env.store, ["front", "middle", "end"])
    paid = _PaidExtractor()
    OpenAICompatibleBulkSemanticExtractor.from_env = classmethod(lambda cls, **kwargs: paid)
    rc, report = _run(cli_env, capsys)
    assert rc == 1
    assert report["status"] == "failed"
    assert paid.calls == []


def test_non_card_session_grounding_fact_passes_real_authority_join():
    class _Store:
        def list_authorized_cards(self, **kwargs):
            return [{"memory_id": "synthetic-card", "content_hash": "synthetic-hash",
                     "currentness": "current"}]

    resolver = GraphFirstResolver.__new__(GraphFirstResolver)
    resolver._store = _Store()
    fact = build_session_grounding_fact(
        project=PROJECT,
        memory_id="synthetic-card",
        content_hash="synthetic-hash",
        shadow_group="shadow_synthetic",
        chunk_key="synthetic-chunk",
    )
    cards, status = resolver._join_graph_candidates(
        SimpleNamespace(project=PROJECT, as_of=None), (fact,)
    )
    assert status == "verified"
    assert len(cards) == 1


def test_source_document_validation_detects_missing_evidence_declaration():
    manifest = {"tool_evidence_bundle_count": 2}
    with pytest.raises(ValueError):
        validate_source_documents([], manifest)
