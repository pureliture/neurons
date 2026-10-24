from __future__ import annotations

from agent_knowledge.couchdb_source.build_cli import (
    MAX_PENDING_ATTEMPTS,
    _apply_pending_attempt_cap,
)


def test_cap_drops_one_session_that_never_finishes() -> None:
    sessions = [
        {"session_id_hash": "sha256:aaa"},
        {"session_id_hash": "sha256:bbb"},
    ]
    attempts = {"sha256:aaa": MAX_PENDING_ATTEMPTS, "sha256:bbb": 1}

    surviving, updated, dropped = _apply_pending_attempt_cap(sessions, attempts)

    assert [s["session_id_hash"] for s in surviving] == ["sha256:bbb"]
    assert dropped == 1
    assert updated == {"sha256:bbb": 2}


def test_cap_increments_and_preserves_attempt_counts() -> None:
    sessions = [{"session_id_hash": "sha256:ccc"}]

    surviving, updated, dropped = _apply_pending_attempt_cap(sessions, {})

    assert dropped == 0
    assert surviving == sessions
    assert updated == {"sha256:ccc": 1}
