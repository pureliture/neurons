"""Adaptive bounded reads and shared authority lookup contract."""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone
import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBResponseTooLarge
from agent_knowledge.llm_brain_core.bulk_authority import authorized_sources_by_hash
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid
from agent_knowledge.llm_brain_core.bulk_streaming import _fetch_with_split


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _members(prefix: str, count: int, body_sizes: list[int]):
    result = []
    for i in range(count):
        body = ("한" * body_sizes[i]).encode("utf-8")
        result.append(SimpleNamespace(
            doc_id=f"{prefix}-{i}", content_hash="sha256:" + hashlib.sha256(body).hexdigest(),
            lane="conversation", ordinal=i, redaction_version="1", body=body.decode("utf-8"),
        ))
    return result

def test_adaptive_split_preserves_order_and_dedupe():
    members = _members("doc", 8, [100000] * 8)
    calls = []

    def get_many(doc_ids, *, page_size, max_response_bytes):
        calls.append(list(doc_ids))
        if len(doc_ids) > 2:
            raise CouchDBResponseTooLarge("too large")
        return {doc_id: {"body": next(m.body for m in members if m.doc_id == doc_id)}
                for doc_id in doc_ids}

    fetched = _fetch_with_split(get_many, members)
    assert list(fetched.keys()) == [m.doc_id for m in members]
    assert all(len(ids) <= 4 for ids in calls[1:])
    assert any(len(ids) <= 2 for ids in calls[1:])
    assert len(calls) > 1


def test_single_oversized_document_fails_closed_with_reason():
    members = _members("doc", 1, [700000])
    def get_many(doc_ids, *, page_size, max_response_bytes):
        raise CouchDBResponseTooLarge("too large")

    with pytest.raises(SourceInvalid, match="response cap|body budget"):
        _fetch_with_split(get_many, members)


def test_shared_authority_lookup_101_hashes_real_pg(isolated_pg_store):
    project = "shared-" + uuid.uuid4().hex[:10]
    hashes = [_h(f"other-{i}-{project}") for i in range(101)]
    target = _h("target-" + project)
    hashes[100] = target
    memory_id = "mem_" + uuid.uuid4().hex[:16]
    envelope = {
        "memory_id": memory_id, "project": project, "card_type": "preference",
        "approval_state": "approved", "steward_proposal_kind": "", "lifecycle_state": "accepted",
        "authorization_status": "active", "currentness": "current", "content_hash": target,
    }
    with isolated_pg_store._scope(write=True) as db:
        db.execute(
            "INSERT INTO memory_cards (memory_id, project, card_type, title, summary, "
            "lifecycle_state, authorization_status, currentness, content_hash, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s::jsonb)",
            [memory_id, project, target, json.dumps(envelope)],
        )
    as_of = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = authorized_sources_by_hash(isolated_pg_store.dsn, project, hashes, as_of=as_of)
    assert result[target][0]["authority_memory_id"] == memory_id


def test_shared_authority_lookup_requires_dsn():
    with pytest.raises(RuntimeError, match="authority DSN unavailable"):
        authorized_sources_by_hash("", "project", [_h("x")])
