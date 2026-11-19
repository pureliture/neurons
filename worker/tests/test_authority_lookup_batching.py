"""Authority lookup must chunk >100 hashes and fail closed without PG."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from agent_knowledge.llm_brain_core.bulk_stream_runner import _pg_authority_by_hash

def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _insert_authorized(store, project: str, content_hash: str) -> str:
    memory_id = "mem_" + uuid.uuid4().hex[:16]
    envelope = {
        "memory_id": memory_id, "project": project, "card_type": "preference",
        "approval_state": "approved", "steward_proposal_kind": "",
        "lifecycle_state": "accepted", "authorization_status": "active",
        "currentness": "current", "content_hash": content_hash,
    }
    with store._scope(write=True) as db:
        db.execute(
            "INSERT INTO memory_cards (memory_id, project, card_type, title, summary, "
            "lifecycle_state, authorization_status, currentness, content_hash, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s::jsonb)",
            [memory_id, project, content_hash, json.dumps(envelope)],
        )
    return memory_id


@pytest.mark.parametrize("count", [101, 201, 4000])
def test_large_hash_sets_are_chunked_without_losing_targets(isolated_pg_store, count):
    project = "batch-" + uuid.uuid4().hex[:10]
    hashes = [_h(f"h-{i}-{project}") for i in range(count)]
    target_100 = _h("target-100-" + project)
    target_last = _h("target-last-" + project)
    hashes[100] = target_100
    hashes[-1 if count > 101 else 99] = target_last
    expected_100 = _insert_authorized(isolated_pg_store, project, target_100)
    expected_last = _insert_authorized(isolated_pg_store, project, target_last)
    as_of = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = _pg_authority_by_hash(isolated_pg_store.dsn, project, hashes, as_of=as_of)
    assert result[target_100][0]["authority_memory_id"] == expected_100
    assert result[target_last][0]["authority_memory_id"] == expected_last


def test_duplicate_across_100_boundary_is_ambiguous(isolated_pg_store):
    project = "batch-" + uuid.uuid4().hex[:10]
    hashes = [_h(f"h-{i}-{project}") for i in range(101)]
    duplicate = _h("duplicate-" + project)
    hashes[99] = duplicate
    hashes[100] = duplicate
    _insert_authorized(isolated_pg_store, project, duplicate)
    _insert_authorized(isolated_pg_store, project, duplicate)
    as_of = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = _pg_authority_by_hash(isolated_pg_store.dsn, project, hashes, as_of=as_of)
    assert duplicate not in result


def test_missing_pg_dsn_fails_closed():
    with pytest.raises(RuntimeError, match="authority DSN unavailable"):
        _pg_authority_by_hash("", "project", [_h("x")])
