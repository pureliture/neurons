"""M1: Hermes provider ingest identity — normalization + acceptance + distinctness.

Hermes는 raw transcript을 generic ``provider_transcript_fixture.v1`` 경로로 ingest되며
(native parser 없음), identity는 정규화된 provider 기준으로 안정적이고 codex/claude와
구분된다. proposal-only 권한 평면은 다른 테스트(test_brain_steward.py)가 다룬다.
"""

from __future__ import annotations

import json

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.historical_import import (
    ImportStatus,
    SourceLocator,
    import_historical_source,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import canonicalize_provider


def _write_hermes_fixture(tmp_path, *, session_id="hermes-1", provider="hermes"):
    payload = {
        "provider": provider,
        "schema_version": "provider_transcript_fixture.v1",
        "session_id": session_id,
        "started_at": "2026-06-29T01:00:00Z",
        "ended_at": "2026-06-29T01:10:00Z",
        "turns": [
            {"role": "user", "text": "한국어로 응답해줘", "timestamp": "2026-06-29T01:00:01Z"},
            {"role": "assistant", "text": "알겠습니다", "timestamp": "2026-06-29T01:00:05Z"},
        ],
    }
    safe_provider = provider.strip().lower() or "x"
    path = tmp_path / f"{safe_provider}-{session_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_canonicalize_provider_normalizes_case_and_whitespace():
    assert canonicalize_provider("Hermes") == "hermes"
    assert canonicalize_provider("  HERMES  ") == "hermes"
    assert canonicalize_provider("hermes") == "hermes"
    # 기존 provider는 이미 lowercase라 정규화가 no-op이어야 한다(회귀 방지).
    assert canonicalize_provider("codex") == "codex"
    assert canonicalize_provider("claude") == "claude"
    assert canonicalize_provider("") == ""


def test_hermes_session_ingest_identity_is_stored(tmp_path):
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(
            provider="hermes",
            source_path=_write_hermes_fixture(tmp_path),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.provider == "hermes"
    assert result.session_id_hash
    sessions = store.find_by_session(
        session_id_hash=result.session_id_hash,
        doc_type=dm.SourceDocType.TRANSCRIPT_SESSION,
    )
    assert len(sessions) == 1
    assert sessions[0]["provider"] == "hermes"


def test_hermes_provider_distinct_from_codex(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid = "shared-session-id"
    hermes = import_historical_source(
        locator=SourceLocator(
            provider="hermes",
            source_path=_write_hermes_fixture(tmp_path, session_id=sid, provider="hermes"),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    codex = import_historical_source(
        locator=SourceLocator(
            provider="codex",
            source_path=_write_hermes_fixture(tmp_path, session_id=sid, provider="codex"),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert hermes.status == ImportStatus.IMPORTED
    assert codex.status == ImportStatus.IMPORTED
    # 같은 raw session_id라도 provider가 다르면 identity가 구분된다.
    assert hermes.session_id_hash != codex.session_id_hash


def test_hermes_provider_casing_yields_stable_identity(tmp_path):
    store = InMemoryCouchDBSourceStore()
    lower = import_historical_source(
        locator=SourceLocator(
            provider="hermes",
            source_path=_write_hermes_fixture(tmp_path, session_id="cap", provider="hermes"),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    mixed = import_historical_source(
        locator=SourceLocator(
            provider="Hermes",
            source_path=_write_hermes_fixture(tmp_path, session_id="cap", provider="Hermes"),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert mixed.status == ImportStatus.IMPORTED
    # "Hermes"와 "hermes"는 동일 identity로 정규화된다(casing drift 없음).
    assert lower.session_id_hash == mixed.session_id_hash
