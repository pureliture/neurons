"""Full synthetic read chain: CLI -> CouchDB/PG/Neo4j -> adapter/search -> MCP.

All data is synthetic and loopback-only. The current resolver test uses the
real MCP JSON-RPC dispatcher in-process; a separate test covers real HTTP
Streamable MCP when the optional SDK is available. No production data, model
API, backup, deploy, or Kubernetes access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
import time
import uuid

import httpx
import psycopg
import pytest
from neo4j import GraphDatabase

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticRelation,
    BulkSemanticSessionInput,
    BulkSemanticSessionResult,
)
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import PgChunkState
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode
from agent_knowledge.llm_brain_core.shadow_search_adapter import (
    PgChunkCompletionReader,
    ShadowSearchAdapter,
)
from agent_knowledge.llm_brain_core.trial_shadow_adapter import TrialShadowGraphAdapter
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")
NEO4J_URI = os.environ.get("TRIAL_NEO4J_URI", "")
NEO4J_PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
COUCH_URL = os.environ.get("TRIAL_COUCH_URL") or os.environ.get("TRIAL_COUCHDB_URL", "")
COUCH_USER = os.environ.get("TRIAL_COUCH_USER") or os.environ.get("TRIAL_COUCHDB_USER", "")
COUCH_PASSWORD = os.environ.get("TRIAL_COUCH_PASSWORD") or os.environ.get("TRIAL_COUCHDB_PASSWORD", "")
pytestmark = pytest.mark.skipif(
    not (PG_DSN and NEO4J_URI and NEO4J_PASSWORD and COUCH_URL and COUCH_USER and COUCH_PASSWORD),
    reason="isolated loopback PG/Neo4j/CouchDB required",
)


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _approved_card(store, *, project: str, label: str, content_hash: str, approve: bool = True):
    span = {
        "card_type": "preference", "project": project, "provider": "hermes", "scope": "project",
        "title": "synthetic card " + label,
        "redacted_summary": "synthetic card for isolated read-chain test",
        "typed_payload": {
            "preference": "synthetic value", "explicitness": "explicit", "repeated_count": 1,
            "confirmation_status": "confirmed", "applies_to": "read_chain",
        },
        "source_ref": {"source_id": "synthetic-source-" + label},
        "span_ref": {"span_id": "synthetic-span-" + label},
        "content_hash": content_hash, "confidence": 0.9, "confidence_basis": "synthetic test",
    }
    steward = BrainStewardService(_NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    if approve:
        steward.candidate_approve(
            candidate_memory_id=memory_id, approved_by="test",
            decision_id="read-chain-" + uuid.uuid4().hex,
        )
    return memory_id


def _update_card(store, memory_id: str, **columns) -> None:
    if not columns:
        return
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute(
            f"UPDATE memory_cards SET {assignments} WHERE memory_id = %s",
            (*columns.values(), memory_id),
        )


def _write_shadow(*, group: str, project: str, memory_id: str, content_hash: str,
                  source_revision: str, entity_type: str = "Preference",
                  entity_name: str = "SyntheticEntity", with_relation: bool = True) -> str:
    chunk_key = "read-chain-" + uuid.uuid4().hex
    payload = {
        "project": project, "graph_group_id": group,
        "canonical_chunk_hash": _digest(chunk_key), "fence_token": 1,
        "source_hash": _digest("source-" + chunk_key),
        "source_revision": source_revision,
        "authority_sources": [{
            "authority_memory_id": memory_id, "content_hash": content_hash,
            "source_entity_types": [entity_type],
        }],
    }
    episode = OntologyEpisode.from_payload(
        event_id="event-" + chunk_key, natural_id="natural-" + chunk_key,
        entity_type="Session", payload=payload,
    )
    item = BulkSemanticSessionInput(session_key=chunk_key, episode=episode, text="synthetic")
    relations = ()
    if with_relation:
        relations = (BulkSemanticRelation(source=entity_name, target="SyntheticTarget",
                                          type="supports", fact="synthetic relation fact"),)
    extraction = BulkSemanticExtractionResult((BulkSemanticSessionResult(
        session_key=chunk_key,
        entities=(BulkSemanticEntity(name=entity_name, type=entity_type,
                                      summary="synthetic entity"),),
        relations=relations,
    ),))
    writer = Neo4jShadowWriter(NEO4J_URI, "neo4j", NEO4J_PASSWORD)
    try:
        report = writer.write_batch([item], extraction)
        assert report.entities_written == 1
    finally:
        writer.close()
    return chunk_key


def _cleanup_shadow(keys: list[str]) -> None:
    writer = Neo4jShadowWriter(NEO4J_URI, "neo4j", NEO4J_PASSWORD)
    try:
        for key in keys:
            writer.delete_synthetic(chunk_key=key)
    finally:
        writer.close()


def _adapter(*groups: str) -> TrialShadowGraphAdapter:
    return TrialShadowGraphAdapter(
        NEO4J_URI, "neo4j", NEO4J_PASSWORD, eligible_shadow_groups=groups,
    )


class _EmptyLegacyAdapter:
    def search_context(self, *, brain_id, query, entity_types=None, limit=10):
        return GraphMemoryResult(status="available", episodes=(), details=("legacy_default",))


def _shadow_receipts(keys: list[str]) -> dict[str, str]:
    driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            rows = session.run(
                "MATCH (n:TrialShadowEntity) WHERE n.chunk_key IN $keys "
                "WITH n ORDER BY n.uid "
                "WITH n.chunk_key AS key, collect(n.receipt) AS receipts "
                "RETURN key, receipts[0] AS receipt", keys=keys,
            )
            return {str(row["key"]): str(row["receipt"] or "") for row in rows}
    finally:
        driver.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ServerThread:
    def __init__(self, app, port: int):
        import uvicorn
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._port = port

    def __enter__(self) -> str:
        self._thread.start()
        base = f"http://127.0.0.1:{self._port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                    return base
            except Exception:
                time.sleep(0.05)
def _resolve_via_mcp(store, adapter, *, project: str, query: str = "synthetic") -> dict:
    """Real MCP JSON-RPC dispatcher in-process; HTTP SDK unavailable offline."""
    from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message

    service = KnowledgeSearchService(
        ledger=object(), retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[], pgvector_store=store, graph_adapter=adapter,
    )
    message = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "brain.resolve",
            "arguments": {
                "project": project, "query": query, "mode": "query",
                "response_mode": "minimal", "limit": 5,
            },
        },
    }
    response = handle_jsonrpc_message(message, service, surface="agent")
    assert "result" in response, response
    result = response["result"]["structuredContent"]
    assert result.get("error_code") in (None, "embedding_unavailable"), result
    return result


def _graph_items(result: dict) -> list[dict]:
    return [item for item in result.get("items", []) if item.get("source") == "graph_card"]


def test_http_chain_approved_card_reaches_mcp_brain_resolve(isolated_pg_store):
    project = "chain-" + uuid.uuid4().hex[:10]
    group = "shadow_" + uuid.uuid4().hex[:12]
    content_hash = _digest("approved-" + project)
    memory_id = _approved_card(isolated_pg_store, project=project, label="ok",
                               content_hash=content_hash)
    key = _write_shadow(group=group, project=project, memory_id=memory_id,
                        content_hash=content_hash, source_revision="rev-approved")
    try:
        result = _resolve_via_mcp(isolated_pg_store, _adapter(group), project=project)
        items = _graph_items(result)
        assert any(item["id"] == memory_id for item in items), result
        assert result["metadata"]["retrieval_path"] == "graph_neo4j", result["metadata"]
    finally:
        _cleanup_shadow([key])


def test_http_chain_unapproved_candidate_fail_closed(isolated_pg_store):
    project = "chain-" + uuid.uuid4().hex[:10]
    group = "shadow_" + uuid.uuid4().hex[:12]
    content_hash = _digest("candidate-" + project)
    memory_id = _approved_card(isolated_pg_store, project=project, label="candidate",
                               content_hash=content_hash, approve=False)
    key = _write_shadow(group=group, project=project, memory_id=memory_id,
                        content_hash=content_hash, source_revision="rev-candidate")
    try:
        result = _resolve_via_mcp(isolated_pg_store, _adapter(group), project=project)
        assert all(item["id"] != memory_id for item in _graph_items(result)), result
    finally:
        _cleanup_shadow([key])


def test_http_chain_expired_stale_hash_mismatch_fail_closed(isolated_pg_store):
    cases = (
        ("expired", {"valid_from": "2019-01-01T00:00:00+00:00",
                     "valid_to": "2020-01-01T00:00:00+00:00"}),
        ("stale", {"currentness": "stale"}),
    )
    keys: list[str] = []
    try:
        for label, changes in cases:
            project = "chain-" + uuid.uuid4().hex[:10]
            group = "shadow_" + uuid.uuid4().hex[:12]
            content_hash = _digest(label + project)
            memory_id = _approved_card(isolated_pg_store, project=project, label=label,
                                       content_hash=content_hash)
            _update_card(isolated_pg_store, memory_id, **changes)
            keys.append(_write_shadow(group=group, project=project, memory_id=memory_id,
                                      content_hash=content_hash, source_revision="rev-" + label))
            result = _resolve_via_mcp(isolated_pg_store, _adapter(group), project=project)
            assert all(item["id"] != memory_id for item in _graph_items(result)), (label, result)
        project = "chain-" + uuid.uuid4().hex[:10]
        group = "shadow_" + uuid.uuid4().hex[:12]
        approved_hash = _digest("approved-" + project)
        memory_id = _approved_card(isolated_pg_store, project=project, label="mismatch",
                                   content_hash=approved_hash)
        keys.append(_write_shadow(group=group, project=project, memory_id=memory_id,
                                  content_hash=_digest("different-" + project),
                                  source_revision="rev-mismatch"))
        result = _resolve_via_mcp(isolated_pg_store, _adapter(group), project=project)
        assert all(item["id"] != memory_id for item in _graph_items(result)), result
    finally:
        _cleanup_shadow(keys)


def test_http_chain_shadow_hidden_and_duplicates_deduped(isolated_pg_store):
    project = "chain-" + uuid.uuid4().hex[:10]
    group = "shadow_" + uuid.uuid4().hex[:12]
    content_hash = _digest("dedupe-" + project)
    memory_id = _approved_card(isolated_pg_store, project=project, label="dedupe",
                               content_hash=content_hash)
    keys = [
        _write_shadow(group=group, project=project, memory_id=memory_id,
                      content_hash=content_hash, source_revision="rev-dedupe")
        for _ in range(2)
    ]
    try:
        hidden = _resolve_via_mcp(isolated_pg_store, _adapter(), project=project)
        assert all(item["id"] != memory_id for item in _graph_items(hidden)), hidden
        shown = _resolve_via_mcp(isolated_pg_store, _adapter(group), project=project)
        assert len([item for item in _graph_items(shown) if item["id"] == memory_id]) == 1, shown
    finally:
        _cleanup_shadow(keys)

def test_http_chain_optin_composite_merges_and_defaults_to_legacy(isolated_pg_store):
    project = "chain-" + uuid.uuid4().hex[:10]
    policy = "full-input." + uuid.uuid4().hex[:6]
    group = cli.shadow_group_ids(f"/project/{project}", policy)[0]
    content_hash = _digest("composite-" + project)
    memory_id = _approved_card(isolated_pg_store, project=project, label="composite",
                               content_hash=content_hash)
    keys = [
        _write_shadow(group=group, project=project, memory_id=memory_id,
                      content_hash=content_hash, source_revision=f"rev-{index}")
        for index in range(2)
    ]
    try:
        shadow = _adapter()
        direct = shadow.search_context(brain_id=f"/project/{project}", query="synthetic",
                                       eligible_shadow_groups=(group,))
        receipts: dict[str, str] = {}
        source_hashes: dict[str, str] = {}
        for episode in direct.episodes:
            episode_payload = episode.payload
            key = str(episode_payload.get("chunk_key") or "")
            if key in keys:
                receipts.setdefault(key, str(episode_payload.get("receipt") or ""))
                source_hashes.setdefault(key, str(episode_payload.get("source_hash") or ""))
        assert all(receipts.get(key) and source_hashes.get(key) for key in keys)
        state = PgChunkState(isolated_pg_store.dsn)
        session_hash = _digest("session-" + project)
        for key in keys:
            source_hash = source_hashes[key]
            state.register_plan(run_id="run-" + project, project=project,
                                session_hash=session_hash, policy_version=policy,
                                source_hash=source_hash,
                                map_digest=_digest("map-" + project), chunk_keys=[key])
            token = state.claim(key=key, owner="synthetic-owner")
            state.complete(key=key, fence_token=token, output_digest=_digest("out-" + key),
                           write_receipt=receipts[key], owner="synthetic-owner")
        expected_source_hashes = set(source_hashes.values())

        def _source_current(_project, source_hash, _session_hash):
            return source_hash in expected_source_hashes

        composite = ShadowSearchAdapter(
            _EmptyLegacyAdapter(), shadow, policy_version=policy,
            authority_store=isolated_pg_store,
            chunk_state=PgChunkCompletionReader(isolated_pg_store.dsn),
            source_hash_validator=_source_current,
        )
        payload = direct.episodes[0].payload
        record = PgChunkCompletionReader(isolated_pg_store.dsn).record(payload["chunk_key"])
        assert record is not None, payload
        assert record["receipt"] == payload["receipt"], (record, payload)
        assert record["project"] == project, record
        assert record["policy_version"] == policy, record
        assert record["source_hash"] == payload["source_hash"], (record, payload)
        assert payload["source_hash"] in expected_source_hashes, payload
        cards = isolated_pg_store.list_authorized_cards(
            project=project, memory_ids=[memory_id], steward_only=True,
        )
        assert cards and cards[0]["content_hash"] == payload["authority_sources"][0]["content_hash"], cards
        assert composite._eligible(direct.episodes[0], project, (group,))
        composite_direct = composite.search_context(brain_id=f"/project/{project}", query="synthetic")
        assert len(composite_direct.episodes) == 1, composite_direct
        shown = _resolve_via_mcp(isolated_pg_store, composite, project=project)
        assert len([item for item in _graph_items(shown) if item["id"] == memory_id]) == 1, shown
        assert shown["metadata"]["shadow_read"] == "eligible", shown

        default_off = ShadowSearchAdapter(_EmptyLegacyAdapter(), shadow)
        hidden = _resolve_via_mcp(isolated_pg_store, default_off, project=project)
        assert all(item["id"] != memory_id for item in _graph_items(hidden)), hidden
        assert "shadow_read" not in hidden["metadata"], hidden
    finally:
        _cleanup_shadow(keys)

def test_http_chain_relation_fact_and_source_revision_preserved():
    project = "chain-" + uuid.uuid4().hex[:10]
    group = "shadow_" + uuid.uuid4().hex[:12]
    content_hash = _digest("meta-" + project)
    key = _write_shadow(group=group, project=project, memory_id="unused-" + uuid.uuid4().hex[:8],
                        content_hash=content_hash, source_revision="rev-17")
    legacy_uid = "legacy-" + uuid.uuid4().hex
    driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            session.run("MERGE (m:TrialLegacyMarker {uid: $uid}) SET m.marker = $marker",
                        uid=legacy_uid, marker="keep-me")
            node = session.run(
                "MATCH (n:TrialShadowEntity {chunk_key: $k}) "
                "RETURN n.entity_type AS entity_type, n.source_revision AS source_revision, "
                "n.source_hash AS source_hash, n.authority_memory_id AS authority_memory_id LIMIT 1",
                k=key,
            ).single()
            assert node["entity_type"] == "Preference", node
            assert node["source_revision"] == "rev-17", node
            assert node["source_hash"], node
            relation = session.run(
                "MATCH (:TrialShadowEntity {chunk_key: $k})-[r:TRIAL_SHADOW_REL]->() "
                "RETURN r.fact AS fact, r.relation_type AS relation_type LIMIT 1", k=key,
            ).single()
            assert relation["fact"] == "synthetic relation fact", relation
            assert relation["relation_type"] == "supports", relation
            assert session.run(
                "MATCH (m:TrialLegacyMarker {marker: 'keep-me'}) RETURN count(m) AS c"
            ).single()["c"] >= 1
        adapter = _adapter(group)
        episodes = adapter.search_context(brain_id=f"/project/{project}",
                                          query="synthetic").episodes
        assert episodes and episodes[0].payload["source_revision"] == "rev-17"
        assert adapter.read_relation_facts()[0]["fact"] == "synthetic relation fact"
    finally:
        _cleanup_shadow([key])
        with driver.session() as session:
            session.run("MATCH (m:TrialLegacyMarker {uid: $uid}) DELETE m", uid=legacy_uid)
        driver.close()


class _FreeExtractor:
    """In-process transport fake; never opens a network/model connection."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._base_url = "https://opencode.ai/zen/go/v1"
        self.model = "hy4-flash-free"
        self.provider = "opencode-go"
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):  # type: ignore[no-untyped-def]
        self.calls.append([item.text for item in batch])
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=f"Synthetic {item.session_key}",
                                             type="Concept", summary="synthetic"),),
                relations=(BulkSemanticRelation(source=f"Synthetic {item.session_key}",
                                                target="SyntheticTarget", type="supports",
                                                fact="synthetic CLI relation"),),
            ) for item in batch
        ))


def _seed_couch(store, project: str, bodies: list[str], session_label: str) -> str:
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, session_label)
    store.put(dm.build_transcript_session_document(session=TranscriptSession(
        session_id_hash=sid, provider=provider, project=project,
        started_at="2026-06-21T00:00:00Z",
    )))
    hashes: list[str] = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index}", session_id_hash=sid, provider=provider,
            project=project, turn_start_index=index, turn_end_index=index, text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=provider, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True},
    ))
    return sid


def test_cli_to_couch_pg_neo4j_projection_and_adapter_read_only(monkeypatch, tmp_path, capsys):
    db = "trialfull" + uuid.uuid4().hex[:10]
    auth = base64.b64encode(f"{COUCH_USER}:{COUCH_PASSWORD}".encode()).decode()
    headers = {"Authorization": "Basic " + auth}
    assert httpx.put(f"{COUCH_URL}/{db}", headers=headers, timeout=10).status_code in (201, 202, 412)
    store = CouchDBHttpSourceStore(base_url=COUCH_URL, db=db, auth_header=headers["Authorization"])
    project = "cli-" + uuid.uuid4().hex[:10]
    policy = "full-input.v1"
    session_label = "cli-session-" + uuid.uuid4().hex
    _seed_couch(store, project, ["FRONT" + "A" * 200, "MIDDLE" + "B" * 200,
                                 "C" * 200 + "END"], session_label)
    extractor = _FreeExtractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(
        cli.OpenAICompatibleBulkSemanticExtractor, "from_env",
        classmethod(lambda cls, **kwargs: extractor),
    )
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", NEO4J_PASSWORD)
    monkeypatch.setenv("TRIAL_PG_DSN", PG_DSN)
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "hy4-flash-free")
    args = [
        "--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
        "--project", project, "--limit", "1", "--stream-mode",
        "--trial-neo4j-writer", "--full-input-policy-version", policy,
    ]
    group = cli.shadow_group_ids(f"/project/{project}", policy)[0] if hasattr(cli, "shadow_group_ids") else ""
    try:
        rc = cli.main(args)
        captured = capsys.readouterr()
        report = json.loads(captured.out or captured.err)
        assert rc == 0, json.dumps(report, sort_keys=True)[:4000]
        assert report["status"] == "ok", report
        assert report["full_input_chunks"] == 3, report
        assert len(extractor.calls) == 1 and len(extractor.calls[0]) == 3
        texts = extractor.calls[0]
        assert "FRONT" in texts[0] and "MIDDLE" in texts[1] and "END" in texts[2]
        with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD)) as driver:
            with driver.session() as session:
                row = session.run(
                    "MATCH (n:TrialShadowEntity) WHERE n.group_id IN $groups "
                    "RETURN count(n) AS c", groups=[group] if group else [],
                ).single()
                assert row["c"] >= 3, row
    finally:
        with GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD)) as driver:
            with driver.session() as session:
                session.run("MATCH (n:TrialShadowEntity) WHERE n.group_id = $g "
                            "DETACH DELETE n", g=group)
        httpx.delete(f"{COUCH_URL}/{db}", headers=headers, timeout=10)
