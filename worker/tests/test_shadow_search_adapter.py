"""Synthetic tests for the opt-in A-decision shadow reader."""

from __future__ import annotations

from types import SimpleNamespace

from agent_knowledge.llm_brain_core.bulk_full_input import shadow_group_ids
from agent_knowledge.llm_brain_core.models import GraphMemoryResult
from agent_knowledge.llm_brain_core.shadow_search_adapter import (
    ShadowSearchAdapter,
    build_shadow_search_adapter_from_env,
)

PROJECT = "synthetic-shadow-project"
POLICY = "full-input.v1"
GROUP = shadow_group_ids(f"/project/{PROJECT}", POLICY)[0]


def _episode(*, mid: str, content_hash: str, group: str = GROUP,
             project: str = PROJECT, key: str = "chunk-1",
             receipt: str = "receipt-1", source_hash: str = "sha256:source") -> SimpleNamespace:
    return SimpleNamespace(payload={
        "authority_sources": [{
            "authority_memory_id": mid,
            "content_hash": content_hash,
        }],
        "shadow_group": group,
        "project": project,
        "chunk_key": key,
        "receipt": receipt,
        "source_hash": source_hash,
    })


class _Legacy:
    def __init__(self, *episodes):
        self.episodes = episodes

    def search_context(self, *, brain_id, query, entity_types=None, limit=10):
        return GraphMemoryResult(status="available", episodes=tuple(self.episodes),
                                 details=("legacy_read",))


class _Shadow:
    def __init__(self, *episodes):
        self.episodes = episodes
        self.groups = None

    def search_context(self, *, brain_id, query, entity_types=None, limit=10,
                       eligible_shadow_groups=None):
        self.groups = tuple(eligible_shadow_groups or ())
        return GraphMemoryResult(status="available", episodes=tuple(self.episodes),
                                 details=("shadow_graph_read",))


class _Authority:
    def __init__(self, approved):
        self.approved = dict(approved)

    def list_authorized_cards(self, *, project, memory_ids, steward_only):
        assert steward_only is True
        return [{"content_hash": self.approved[mid]} for mid in memory_ids
                if mid in self.approved]


class _Chunks:
    def __init__(self, records):
        self.records = records

    def record(self, key):
        return self.records.get(key)

    def completion_receipt(self, key):
        row = self.record(key)
        return str(row["receipt"] or "") if row else ""

    def is_complete(self, key):
        return self.record(key) is not None


def _record(*, receipt="receipt-1", project=PROJECT, source_hash="sha256:source"):
    return {
        "receipt": receipt,
        "project": project,
        "policy_version": POLICY,
        "source_hash": source_hash,
        "session_hash": "sha256:session",
        "status": "completed",
    }


def test_shadow_reader_defaults_to_legacy_when_no_group_selected():
    old = _episode(mid="old", content_hash="sha256:old")
    adapter = ShadowSearchAdapter(_Legacy(old), _Shadow(_episode(mid="new", content_hash="sha256:new")))
    result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
    assert [e.payload["authority_sources"][0]["authority_memory_id"] for e in result.episodes] == ["old"]


def test_shadow_reader_merges_eligible_episode_and_dedupes_card_identity():
    old = _episode(mid="shared", content_hash="sha256:shared")
    duplicate = _episode(mid="shared", content_hash="sha256:shared", key="chunk-2", receipt="receipt-2")
    new = _episode(mid="new", content_hash="sha256:new")
    shadow = _Shadow(duplicate, new)
    adapter = ShadowSearchAdapter(
        _Legacy(old), shadow, eligible_groups=(GROUP,), policy_version=POLICY,
        authority_store=_Authority({"shared": "sha256:shared", "new": "sha256:new"}),
        chunk_state=_Chunks({
            "chunk-2": _record(receipt="receipt-2"),
            "chunk-1": _record(),
        }),
        source_hash_validator=lambda _project, source_hash, _session: source_hash == "sha256:source",
    )
    result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
    ids = [e.payload["authority_sources"][0]["authority_memory_id"] for e in result.episodes]
    assert ids == ["shared", "new"]
    assert shadow.groups == (GROUP,)
    assert "shadow_merge_eligible" in result.details


def test_shadow_reader_rejects_unapproved_wrong_group_project_hash_receipt_and_source_change():
    cases = (
        (_episode(mid="x", content_hash="sha256:x", group="other-group"), {}),
        (_episode(mid="x", content_hash="sha256:x", project="other-project"), {}),
        (_episode(mid="x", content_hash="sha256:x"), {"shared": "sha256:other"}),
        (_episode(mid="x", content_hash="sha256:x", receipt="stale"), {}),
        (_episode(mid="x", content_hash="sha256:x", source_hash="sha256:changed"), {}),
    )
    for episode, approved in cases:
        adapter = ShadowSearchAdapter(
            _Legacy(), _Shadow(episode), eligible_groups=(GROUP,),
            authority_store=_Authority({"x": "sha256:x", **approved}),
            chunk_state=_Chunks({"chunk-1": _record()}),
            source_hash_validator=lambda _project, source_hash, _session: source_hash == "sha256:source",
        )
        result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
        assert result.episodes == ()



def test_factory_defaults_to_legacy_without_explicit_opt_in():
    legacy = _Legacy()
    assert build_shadow_search_adapter_from_env(legacy, None, {}) is legacy


def test_factory_fails_closed_when_opt_in_prerequisites_missing():
    legacy = _Legacy()
    environ = {"NEURON_BULK_SHADOW_READ_ELIGIBLE": "1"}
    assert build_shadow_search_adapter_from_env(legacy, None, environ) is legacy


class _RaisingShadow:
    def search_context(self, **kwargs):
        raise RuntimeError("synthetic shadow outage")


def test_shadow_backend_failure_preserves_legacy_with_explicit_marker():
    old = _episode(mid="old", content_hash="sha256:old")
    adapter = ShadowSearchAdapter(
        _Legacy(old), _RaisingShadow(), eligible_groups=(GROUP,),
        authority_store=_Authority({}), source_hash_validator=lambda *_args: True,
    )
    result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
    assert [e.payload["authority_sources"][0]["authority_memory_id"] for e in result.episodes] == ["old"]
    assert "shadow_unavailable" in result.details


def test_missing_authority_or_source_validator_never_exposes_shadow():
    old = _episode(mid="old", content_hash="sha256:old")
    shadow = _Shadow(_episode(mid="new", content_hash="sha256:new"))
    for kwargs in (
        {"source_hash_validator": lambda *_args: True},
        {"authority_store": _Authority({"new": "sha256:new"})},
    ):
        adapter = ShadowSearchAdapter(
            _Legacy(old), shadow, eligible_groups=(GROUP,), chunk_state=_Chunks({"chunk-1": _record()}),
            **kwargs,
        )
        result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
        assert [e.payload["authority_sources"][0]["authority_memory_id"] for e in result.episodes] == ["old"]
        assert "shadow_eligibility_unavailable" in result.details


def test_revocation_between_validation_and_merge_is_not_exposed():
    calls = {"n": 0}

    def _revoking_validator(_project, _source_hash, _session_hash):
        calls["n"] += 1
        return calls["n"] == 1

    adapter = ShadowSearchAdapter(
        _Legacy(), _Shadow(_episode(mid="new", content_hash="sha256:new")),
        eligible_groups=(GROUP,), policy_version=POLICY,
        authority_store=_Authority({"new": "sha256:new"}),
        chunk_state=_Chunks({"chunk-1": _record()}),
        source_hash_validator=_revoking_validator,
    )
    result = adapter.search_context(brain_id=f"/project/{PROJECT}", query="synthetic")
    assert result.episodes == ()
    assert "shadow_merge_rejected" in result.details
    assert calls["n"] == 2


def test_factory_returns_shadow_search_adapter_when_opt_in_prerequisites_present():
    legacy = _Legacy()
    store = SimpleNamespace(dsn="postgresql://synthetic-user:synthetic-pw@127.0.0.1:5432/synthetic")
    environ = {
        "NEURON_BULK_SHADOW_READ_ELIGIBLE": "1",
        "LLM_BRAIN_BULK_FULL_INPUT_POLICY_VERSION": POLICY,
        "LLM_BRAIN_NEO4J_URI": "bolt://127.0.0.1:7687",
        "LLM_BRAIN_NEO4J_PASSWORD": "synthetic-neo4j-password",
        "COUCHDB_URL": "http://127.0.0.1:5984",
        "COUCHDB_DB": "synthetic_db",
        "COUCHDB_USER": "admin",
        "COUCHDB_PASSWORD": "synthetic-couch-password",
    }
    adapter = build_shadow_search_adapter_from_env(legacy, store, environ)
    assert isinstance(adapter, ShadowSearchAdapter)
    assert adapter.policy_version == POLICY
