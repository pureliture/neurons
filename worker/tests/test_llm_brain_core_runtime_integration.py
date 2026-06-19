from __future__ import annotations

import json

import pytest

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core import (
    BrainReadService,
    FakeGraphMemoryAdapter,
    LedgerSessionMemoryArtifactStore,
    LedgerSourceRefCatalog,
)
from agent_knowledge.llm_brain_core.runtime import (
    brain_event_from_ingress_payload,
    build_runtime_brain_service,
    episode_from_memory_card,
    materialize_artifact_from_couchdb_source,
    replay_ingress_events,
    source_ref_from_catalog_event,
)
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel
from agent_knowledge.session_memory.transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptChunk,
    TranscriptSession,
)


PROJECT = "neurons"
PROVIDER = "codex"


def test_runtime_integration_session_artifact_card_source_event_to_contextpack(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    source_catalog = LedgerSourceRefCatalog(ledger)
    source_store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_couchdb_source(source_store)

    artifact = materialize_artifact_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=source_store,
        artifact_store=artifact_store,
    )
    source_catalog.register(
        source_ref_from_catalog_event(
            {
                "source_ref_id": "src_runtime_design",
                "device_id_hash": _h("device-a"),
                "root_id": "project-root",
                "relative_path_hash": _h("docs/design.md"),
                "content_hash": _h("source-content"),
                "mtime": "2026-06-19T00:00:00Z",
                "size": 100,
                "sync_policy": "derived_only",
                "derived_summary": "Runtime integration design note.",
            }
        )
    )
    _upsert_runtime_cards(ledger)

    event_report = replay_ingress_events(
        [
            {
                "eventId": "evt_session_materialized",
                "idempotencyKey": "brain-event:session-materialized",
                "contentHash": artifact.content_hash,
                "project": PROJECT,
                "provider": PROVIDER,
                "session_id_hash": session_id_hash,
            },
            {
                "eventId": "evt_session_materialized_dup",
                "idempotencyKey": "brain-event:session-materialized",
                "contentHash": artifact.content_hash,
                "project": PROJECT,
                "provider": PROVIDER,
                "session_id_hash": session_id_hash,
            },
        ],
        device_id_hash=_h("device-a"),
    )

    service = build_runtime_brain_service(
        project=PROJECT,
        artifact_store=artifact_store,
        read_model=LegacyLedgerBrainReadModel(ledger),
        source_catalog=source_catalog,
        graph_adapter=FakeGraphMemoryAdapter([]),
    )
    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/llm-brain-core-design",
        current_files=["worker/lib/agent_knowledge/llm_brain_core/runtime.py"],
        current_request="continue runtime integration",
        project=PROJECT,
    ).to_dict()
    evidence = service.brain_evidence_get(
        __import__("agent_knowledge.llm_brain_core.models", fromlist=["EvidenceRequest"]).EvidenceRequest(
            source_ref_id="src_runtime_design",
            requesting_device_id_hash=_h("device-a"),
        )
    )

    assert artifact_store.get(artifact.artifact_id).artifact_id == artifact.artifact_id
    assert event_report["applied"] == ["evt_session_materialized"]
    assert event_report["duplicates"] == ["evt_session_materialized_dup"]
    assert pack["current_task"] == "Wire core runtime to Ledger and CouchDB source fixtures"
    assert "ContextPack through Ledger read model" in pack["last_stopped_at"]
    assert pack["memory_status"]["artifact_count"] == 1
    assert pack["memory_status"]["card_count"] >= 4
    assert pack["bridge_status"]["status"] == "disabled"
    assert evidence["resolution_state"] == "derived_only"

    serialized = json.dumps({"pack": pack, "evidence": evidence}, sort_keys=True)
    assert "/Users/" not in serialized
    assert "raw transcript" not in serialized.lower()
    assert "TOKEN" not in serialized
    assert "dataset_id" not in serialized
    assert "document_id" not in serialized


def test_runtime_connected_incident_drift_persona_paths(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    source_catalog = LedgerSourceRefCatalog(ledger)
    _upsert_runtime_cards(ledger)
    graph = FakeGraphMemoryAdapter(
        [
            _episode("Incident", "incident:ack", {"incident_id": "incident:ack", "symptom": "ack_pending grows"}),
            _episode("Attempt", "attempt:ack", {"incident_id": "incident:ack", "attempt": "Inspect consumer counters"}),
            _episode("Fix", "fix:ack", {"incident_id": "incident:ack", "fix": "Remove broad natural-key scan"}),
            _episode("Verification", "verification:ack", {"incident_id": "incident:ack", "verification": "pending returned to zero"}),
        ]
    )
    service = build_runtime_brain_service(
        project=PROJECT,
        artifact_store=artifact_store,
        read_model=LegacyLedgerBrainReadModel(ledger),
        source_catalog=source_catalog,
        graph_adapter=graph,
    )

    incident = service.brain_incident_search(symptom="ack_pending grows", project=PROJECT)
    drift = service.brain_drift_explain(subject="core memory authority", project=PROJECT)
    persona = service.brain_persona_check(plan="Finalize architecture before implementation.", project=PROJECT)

    assert incident["reusable_fixes"][0]["fixes"] == ["Remove broad natural-key scan"]
    assert drift["status"] == "explained"
    assert drift["prior_decisions"][0]["memory_id"] == "mem_runtime_decision_old"
    assert drift["current_decisions"][0]["memory_id"] == "mem_runtime_decision_new"
    assert persona["status"] == "aligned"


def test_current_task_selection_ignores_card_metadata_terms():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_task_match",
                "task",
                "Task restore ContextPack",
                {
                    "task_state": "Task restore ContextPack",
                    "next_action": "Continue graph projection",
                    "status": "open",
                },
            )
            | {"updated_at": "2026-06-19T00:00:00Z"},
            _card(
                "mem_task_metadata_only",
                "task",
                "Unrelated cleanup",
                {
                    "task_state": "Unrelated cleanup",
                    "next_action": "Do not select this from metadata",
                    "status": "open",
                },
            )
            | {"updated_at": "2026-06-19T00:01:00Z"},
        ]
    )

    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/llm-brain-core-design",
        current_files=[],
        current_request="task",
        project=PROJECT,
    )

    assert pack.current_task == "Task restore ContextPack"


def test_memory_card_projection_preserves_brain_id_for_graph_grouping():
    card = _card(
        "mem_graph_project",
        "task",
        "Graph projection task",
        {"task_state": "Graph projection task", "next_action": "Project card", "status": "open"},
    )

    episode = episode_from_memory_card(card)

    assert episode.payload["brain_id"] == f"/project/{PROJECT}"
    assert episode.payload["project"] == PROJECT


def test_brain_context_resolve_cli_reads_ledger_backed_core(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(ledger_path)
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    source_store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_couchdb_source(source_store)
    materialize_artifact_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=source_store,
        artifact_store=artifact_store,
    )
    _upsert_runtime_cards(ledger)

    rc = neuron_main(
        [
            "brain-context-resolve",
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--repository",
            "/Users/example/Projects/neurons",
            "--branch",
            "codex/llm-brain-core-design",
            "--current-request",
            "continue runtime integration",
            "--current-file",
            "worker/lib/agent_knowledge/llm_brain_core/runtime.py",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert report["schema_version"] == "llm_brain_context_resolve.v1"
    assert report["status"] == "ok"
    pack = report["context_pack"]
    assert pack["current_task"] == "Wire core runtime to Ledger and CouchDB source fixtures"
    assert pack["bridge_status"]["status"] == "disabled"
    assert "/Users/" not in json.dumps(report, sort_keys=True)


def test_brain_event_mapping_accepts_existing_queue_shape():
    envelope = brain_event_from_ingress_payload(
        {
            "eventId": "evt_queue_1",
            "idempotencyKey": "ingress:session-memory:1",
            "contentHash": _h("payload"),
            "payload": {
                "document": {
                    "metadata": {
                        "knowledge_id": "kn_session",
                        "project": PROJECT,
                        "provider": PROVIDER,
                        "session_id_hash": _sid(),
                    }
                }
            },
            "supersedes": ["evt_prior"],
        },
        device_id_hash=_h("device-a"),
    )

    assert envelope.event_id == "evt_queue_1"
    assert envelope.payload["supersedes"] == ["evt_prior"]
    assert envelope.idempotency_key == "ingress:session-memory:1"
    assert envelope.payload == {
        "target_id": "kn_session",
        "payload_hash": _h("payload"),
        "project": PROJECT,
        "provider": PROVIDER,
        "session_id_hash": _sid(),
        "kind": "",
        "supersedes": ["evt_prior"],
    }


def test_runtime_rejects_inconsistent_couchdb_session_scope():
    source_store = InMemoryCouchDBSourceStore()
    session_id_hash = _seed_couchdb_source(source_store)
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_wrong_project",
        session_id_hash=session_id_hash,
        provider=PROVIDER,
        project="other-project",
        turn_start_index=2,
        turn_end_index=2,
        text="wrong project",
    )
    source_store.put(dm.build_conversation_chunk_document(chunk=chunk))

    with pytest.raises(ValueError, match="inconsistent project"):
        materialize_artifact_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        )


def test_runtime_source_ref_event_bounds_invalid_size():
    record = source_ref_from_catalog_event(
        {
            "source_ref_id": "src_bad_size",
            "device_id_hash": _h("device-a"),
            "root_id": "project-root",
            "relative_path_hash": _h("docs/design.md"),
            "content_hash": _h("source-content"),
            "size": "not-a-number",
            "sync_policy": "metadata_only",
        }
    )

    assert record.size == 0


def test_runtime_episode_from_memory_card_rejects_malformed_identity_and_tolerates_scalar_derived_from():
    card = _card(
        "mem_scalar_derived_from",
        "task",
        "Scalar derived_from should not crash",
        {
            "task_state": "Scalar derived_from should not crash",
            "next_action": "Keep source_event_ids empty",
            "blocker": "",
            "owner_hint": "neurons",
            "status": "open",
        },
    )
    card["derived_from"] = "evt_scalar"
    episode = episode_from_memory_card(card)
    assert episode.source_event_ids == ()

    malformed = dict(card)
    malformed["memory_id"] = ""
    with pytest.raises(ValueError, match="memory_id"):
        episode_from_memory_card(malformed)


def test_context_project_filter_does_not_treat_missing_project_as_wildcard():
    from agent_knowledge.llm_brain_core import BrainReadService, InMemorySessionMemoryArtifactStore

    card = _card(
        "mem_missing_project",
        "task",
        "Wrong project task",
        {
            "task_state": "Wrong project task",
            "next_action": "Should not appear",
            "blocker": "",
            "owner_hint": "other",
            "status": "open",
        },
    )
    card.pop("project")
    service = BrainReadService(
        artifact_store=InMemorySessionMemoryArtifactStore(),
        memory_cards=[card],
    )

    pack = service.brain_context_resolve(
        repository="neurons",
        branch="main",
        current_files=[],
        current_request="anything",
        project=PROJECT,
    ).to_dict()

    assert pack["current_task"] == ""
    assert pack["memory_status"]["card_count"] == 0


def _seed_couchdb_source(store: InMemoryCouchDBSourceStore) -> str:
    session = TranscriptSession(
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-19T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_runtime_001",
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        text="User asked to continue runtime integration with Ledger-backed storage.",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    coverage = dm.build_coverage_manifest_document(
        session_id_hash=_sid(),
        provider=PROVIDER,
        project=PROJECT,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        tool_evidence_coverage_hashes=[],
    )
    store.put(coverage)
    store_tool_evidence_bundles(
        [
            ToolEvidenceSummaryRecord(
                session_id_hash=_sid(),
                provider=PROVIDER,
                project=PROJECT,
                category="test_result",
                outcome="pass",
                tool_name="uv",
                command_summary="uv run pytest -q",
                redacted_summary="Runtime integration fixture passed.",
                evidence_index=0,
            )
        ],
        store=store,
    )
    return _sid()


def _upsert_runtime_cards(ledger: Ledger) -> None:
    ledger.upsert_llm_brain_memory_card(
        _card(
            "mem_runtime_task",
            "task",
            "Wire core runtime to Ledger and CouchDB source fixtures",
            {
                "task_state": "Wire core runtime to Ledger and CouchDB source fixtures",
                "next_action": "Resolve ContextPack through Ledger read model",
                "blocker": "",
                "owner_hint": "neurons",
                "status": "open",
            },
        )
    )
    ledger.upsert_llm_brain_memory_card(
        _card(
            "mem_runtime_decision_old",
            "decision",
            "Use standalone local store for core artifacts",
            {
                "decision": "Use standalone local store for core artifacts.",
                "rationale": "It was faster to test.",
                "alternatives": [],
                "consequence": "This drifted from neurons store authority.",
                "authority_ref": "superseded",
            },
            currentness="superseded",
            superseded_by=["mem_runtime_decision_new"],
        )
    )
    ledger.upsert_llm_brain_memory_card(
        _card(
            "mem_runtime_decision_new",
            "decision",
            "Use Ledger-backed store pattern for core artifacts",
            {
                "decision": "Use Ledger-backed store pattern for core artifacts.",
                "rationale": "Runtime integration must reuse neurons authority surfaces.",
                "alternatives": ["standalone local store"],
                "consequence": "brain_context_resolve can read one Ledger-backed surface.",
                "authority_ref": "specs/llm-brain-core-v1/design.md",
            },
            supersedes=["mem_runtime_decision_old"],
        )
    )
    ledger.upsert_llm_brain_memory_card(
        _card(
            "mem_runtime_drift",
            "drift",
            "Core memory authority changed",
            {
                "subject": "core memory authority",
                "expected_state": "standalone local store",
                "observed_state": "Ledger-backed store pattern",
                "drift_kind": "implementation_boundary",
                "severity": "medium",
                "authority_lane": "implementation",
                "source_precedence_rank": 0.9,
                "resolution_action": "mark_superseded",
                "suggested_action": "Keep runtime persistence in Ledger adapter.",
                "basis_refs": ["src_runtime_design"],
            },
        )
    )
    ledger.upsert_llm_brain_memory_card(
        _card(
            "mem_runtime_persona",
            "preference",
            "User prefers architecture before implementation.",
            {
                "preference": "User prefers architecture before implementation.",
                "explicitness": "explicit",
                "repeated_count": 1,
                "confirmation_status": "confirmed",
                "applies_to": "global",
            },
        )
    )


def _card(memory_id, card_type, summary, typed_payload, currentness="current", supersedes=None, superseded_by=None):
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{PROJECT}",
        "card_type": card_type,
        "scope": "project",
        "project": PROJECT,
        "provider": PROVIDER,
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": currentness,
        "confidence": 0.9,
        "confidence_basis": "runtime fixture",
        "source_refs": [{"source_ref_id": "src_runtime_design", "content_hash": _h("source-content")}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": list(supersedes or []),
        "superseded_by": list(superseded_by or []),
        "conflicts": [],
        "active_until": "",
        "typed_payload": typed_payload,
    }


def _episode(entity_type, natural_id, payload):
    from agent_knowledge.llm_brain_core.models import OntologyEpisode

    return OntologyEpisode.from_payload(
        event_id=f"evt_{natural_id.replace(':', '_')}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        source_ref_ids=["src_runtime_design"],
    )


def _sid() -> str:
    return dm.build_session_id_hash(PROVIDER, "runtime-session")


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()
