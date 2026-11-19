"""Real-PG contract: authority lookup is SQL hash-scoped and ambiguity-aware."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from agent_knowledge.session_memory.brain_steward import BrainStewardService


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name):
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def _h(text: str) -> str:
    return "sha256:" + __import__("hashlib").sha256(text.encode()).hexdigest()


def _card(store, project: str, content_hash: str, *, approve: bool = True) -> str:
    span = {
        "card_type": "preference", "project": project, "provider": "hermes", "scope": "project",
        "title": "synthetic", "redacted_summary": "synthetic",
        "typed_payload": {
            "preference": "synthetic", "explicitness": "explicit", "repeated_count": 1,
            "confirmation_status": "confirmed", "applies_to": "authority_lookup",
        },
        "source_ref": {"source_id": "synthetic-" + uuid.uuid4().hex},
        "span_ref": {"span_id": "synthetic-" + uuid.uuid4().hex},
        "content_hash": content_hash, "confidence": 0.9, "confidence_basis": "synthetic test",
    }
    steward = BrainStewardService(_NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    if approve:
        steward.candidate_approve(
            candidate_memory_id=memory_id, approved_by="test",
            decision_id="lookup-" + uuid.uuid4().hex,
        )
    return memory_id


def _lookup(store, project: str, hashes: list[str]):
    return store.find_authorized_by_content_hashes(
        project=project, content_hashes=hashes,
        as_of=datetime.now(timezone.utc) + timedelta(seconds=60),
    )


def test_target_is_found_when_it_is_not_first_in_project_order(isolated_pg_store):
    project = "lookup-" + uuid.uuid4().hex[:10]
    for _ in range(8):
        _card(isolated_pg_store, project, _h(uuid.uuid4().hex))
    target_hash = _h("target")
    target = _card(isolated_pg_store, project, target_hash)
    result = _lookup(isolated_pg_store, project, [target_hash])
    assert result["by_hash"][target_hash] == [target]
    assert target_hash not in result["ambiguous_hashes"]


def test_target_after_arbitrary_100_card_cutoff_is_found(isolated_pg_store):
    project = "lookup-" + uuid.uuid4().hex[:10]
    for _ in range(101):
        _card(isolated_pg_store, project, _h(uuid.uuid4().hex))
    target_hash = _h("after-cutoff")
    target = _card(isolated_pg_store, project, target_hash)
    result = _lookup(isolated_pg_store, project, [target_hash])
    assert result["by_hash"][target_hash] == [target]
    assert target_hash not in result["ambiguous_hashes"]


def test_ambiguity_at_101st_card_is_detected_in_same_snapshot(isolated_pg_store):
    project = "lookup-" + uuid.uuid4().hex[:10]
    for _ in range(100):
        _card(isolated_pg_store, project, _h(uuid.uuid4().hex))
    target_hash = _h("ambiguous")
    _card(isolated_pg_store, project, target_hash)
    _card(isolated_pg_store, project, target_hash)
    result = _lookup(isolated_pg_store, project, [target_hash])
    assert target_hash in result["ambiguous_hashes"]
    assert target_hash not in result["by_hash"]


def test_policy_filters_and_project_scope_are_preserved(isolated_pg_store):
    project = "lookup-" + uuid.uuid4().hex[:10]
    other = "other-" + uuid.uuid4().hex[:10]
    owned = _card(isolated_pg_store, project, _h("owned"))
    _card(isolated_pg_store, project, _h("unapproved"), approve=False)
    expired = _card(isolated_pg_store, project, _h("expired"))
    with isolated_pg_store._scope(write=True) as db:
        db.execute(
            "UPDATE memory_cards SET valid_from = %s, valid_to = %s WHERE memory_id = %s",
            ("2019-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", expired),
        )
    _card(isolated_pg_store, other, _h("cross"))
    hashes = [_h("owned"), _h("unapproved"), _h("expired"), _h("cross"), _h("invalid")]
    result = _lookup(isolated_pg_store, project, hashes)
    assert result["by_hash"] == {_h("owned"): [owned]}
    assert result["ambiguous_hashes"] == set()
