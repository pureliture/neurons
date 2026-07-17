"""M4: Qdrant hit ledger-join authority gate.

Resolution routes through the canonical ledger predicate
(authorize_document_by_content_hash -> _authorize_knowledge_item), so the mirror
cannot diverge: superseded / disabled / not-indexed / authorization-revoked /
disabled-dataset records are dropped exactly as for a local read. Resolved hits
are flipped to authoritative and have scope reconciled from the canonical record;
unresolved hits are dropped.
"""

from __future__ import annotations

from agent_knowledge.ledger import Ledger
from agent_knowledge.rag_ingress.retired_index_bridge import IndexStatus
from agent_knowledge.rag_ingress.qdrant_authority_join import (
    LedgerContentHashAuthorityResolver,
    join_mirror_hits_to_authority,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    FOUNDATION_DIRECT_WRITE_CONTRACT,
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _insert_item(ledger: Ledger, *, content_hash: str, **overrides):
    fields = {
        "knowledge_id": "k_" + content_hash[-12:],
        "content_hash": content_hash,
        "provider": "claude",
        "project": "neurons",
        "domain": "agent_memory",
        "type": "approved_memory_card",
        "title": "t",
        "summary": "s",
        "status": "indexed",
        "authorization_status": "active",
        "disabled_at": "",
        "supersedes": "",
        "valid_until": "",
        "privacy_level": "private",
        # non-empty unknown dataset id -> _dataset_is_enabled returns True
        "index_target_id": "ds1",
        "index_document_id": "doc_" + content_hash[-12:],
    }
    fields.update(overrides)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with ledger._connect() as connection:
        connection.execute(
            f"INSERT INTO knowledge_items ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )


def _hit(content_hash: str, *, target_profile: str = "derived-memory-items") -> dict:
    return {
        "result_type": "searchable_mirror",
        "authority": "searchable_runtime_mirror",
        "target_profile": target_profile,
        "source_ref": "workspace:approved_memory_card:" + content_hash,
        "content_hash": content_hash,
        "summary": "candidate",
        "canonical_resolution_required": True,
        "authority_join_status": "not_checked",
    }


# --- canonical predicate via content_hash (the blocker fix) ------------------

def test_authorize_by_content_hash_accepts_authorized_row(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _insert_item(ledger, content_hash="sha256:ok")
    assert ledger.authorize_document_by_content_hash("sha256:ok") is not None


def test_authorize_by_content_hash_drops_superseded_and_disabled_and_unindexed(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _insert_item(ledger, content_hash="sha256:superseded", supersedes="some-other-id")
    _insert_item(ledger, content_hash="sha256:disabled", disabled_at="2020-01-01T00:00:00Z")
    _insert_item(ledger, content_hash="sha256:prepared", status="prepared")
    _insert_item(ledger, content_hash="sha256:revoked", authorization_status="disabled")
    _insert_item(ledger, content_hash="sha256:nodataset", index_target_id="")

    for ch in ("sha256:superseded", "sha256:disabled", "sha256:prepared", "sha256:revoked", "sha256:nodataset"):
        assert ledger.authorize_document_by_content_hash(ch) is None, ch
    # absent / empty
    assert ledger.authorize_document_by_content_hash("sha256:missing") is None
    assert ledger.authorize_document_by_content_hash("") is None


# --- resolver delegation + join reconciliation ------------------------------

class _RecordingLedger:
    def __init__(self, by_hash):
        self._by_hash = by_hash
        self.calls = []

    def authorize_document_by_content_hash(self, content_hash, *, filters=None):
        self.calls.append((content_hash, filters))
        return self._by_hash.get(content_hash)


def test_resolver_delegates_to_canonical_gate_and_drops_on_none():
    ledger = _RecordingLedger({"sha256:a": {"status": "indexed", "privacy_level": "private"}})
    resolver = LedgerContentHashAuthorityResolver(ledger)
    assert resolver.resolve(_hit("sha256:a")) is not None
    assert resolver.resolve(_hit("sha256:b")) is None  # gate returned None
    assert resolver.resolve(_hit("")) is None  # never calls gate for empty
    assert ("sha256:a", None) in ledger.calls


def test_join_reconciles_scope_from_authoritative_record():
    ledger = _RecordingLedger(
        {"sha256:a": {"privacy_level": "secret-tier", "project": "p2", "provider": "codex", "currentness": "current"}}
    )
    [joined] = join_mirror_hits_to_authority([_hit("sha256:a")], resolver=LedgerContentHashAuthorityResolver(ledger))
    assert joined["authority_join_status"] == "resolved"
    assert joined["canonical_resolution_required"] is False
    assert joined["authority"] == "local_ledger"
    # scope is taken from the authoritative record, not the mirror payload
    assert joined["privacy_class"] == "secret-tier"
    assert joined["project"] == "p2"
    assert joined["provider"] == "codex"


def test_join_drops_hit_when_mirror_privacy_disagrees_with_authority():
    # mirror labeled the hit 'private' but the canonical record is 'secret-tier'
    ledger = _RecordingLedger({"sha256:a": {"privacy_level": "secret-tier"}})
    hit = {**_hit("sha256:a"), "privacy_class": "private"}
    # default: dropped (never relabel-and-serve)
    assert join_mirror_hits_to_authority([hit], resolver=LedgerContentHashAuthorityResolver(ledger)) == []
    # not-dropping: flagged privacy_mismatch, not resolved
    [flagged] = join_mirror_hits_to_authority(
        [hit], resolver=LedgerContentHashAuthorityResolver(ledger), drop_unresolved=False
    )
    assert flagged["authority_join_status"] == "privacy_mismatch"
    assert flagged["canonical_resolution_required"] is True


def test_unresolved_hit_dropped_by_default_kept_flagged_otherwise():
    ledger = _RecordingLedger({})
    assert join_mirror_hits_to_authority([_hit("sha256:a")], resolver=LedgerContentHashAuthorityResolver(ledger)) == []
    [flagged] = join_mirror_hits_to_authority(
        [_hit("sha256:a")], resolver=LedgerContentHashAuthorityResolver(ledger), drop_unresolved=False
    )
    assert flagged["authority_join_status"] == "unresolved"
    assert flagged["canonical_resolution_required"] is True


# --- end-to-end: real ledger, authorized survives, superseded dropped --------

def _adapter():
    return QdrantDoclingMirrorAdapter(
        client=InMemoryQdrantClient(),
        direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=32),
    )


def _submit(adapter, body: str):
    doc = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/x.md",
        privacy_class="private",
        body=body,
        filename="x.md",
        metadata={"project": "neurons"},
    )
    result = adapter.submit_document(doc)
    assert result.status == IndexStatus.INDEXED
    return doc


def test_end_to_end_authorized_survives_superseded_and_missing_dropped(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    adapter = _adapter()
    good = _submit(adapter, "decision about ledger authority alpha")
    stale = _submit(adapter, "decision about ledger authority beta superseded")
    _missing = _submit(adapter, "decision about ledger authority gamma unbacked")

    _insert_item(ledger, content_hash=good.content_hash)  # authorized
    _insert_item(ledger, content_hash=stale.content_hash, supersedes="prev-id")  # superseded -> dropped
    # gamma intentionally has no ledger row -> dropped

    hits = adapter.query_mirror_candidates(
        "decision", target_profile="derived-memory-items", privacy_class="private", limit=10
    )
    joined = join_mirror_hits_to_authority(hits, resolver=LedgerContentHashAuthorityResolver(ledger))
    survivors = {h["content_hash"] for h in joined}
    assert good.content_hash in survivors
    assert stale.content_hash not in survivors
    assert _missing.content_hash not in survivors
    assert all(h["authority_join_status"] == "resolved" for h in joined)
