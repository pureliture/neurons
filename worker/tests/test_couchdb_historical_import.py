from __future__ import annotations

import json

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.historical_import import (
    ImportStatus,
    SourceLocator,
    import_historical_source,
    import_historical_sources,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore


def _write_codex_fixture(tmp_path, *, session_id="sess-1", user_text="please run the migration"):
    payload = {
        "provider": "codex",
        "schema_version": "provider_transcript_fixture.v1",
        "session_id": session_id,
        "started_at": "2026-06-17T01:00:00Z",
        "ended_at": "2026-06-17T01:10:00Z",
        "turns": [
            {"role": "user", "text": user_text, "timestamp": "2026-06-17T01:00:01Z"},
            {"role": "assistant", "text": "done; 12 passed", "timestamp": "2026-06-17T01:00:05Z"},
        ],
    }
    path = tmp_path / f"codex-{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_import_writes_session_chunks_and_coverage(tmp_path):
    store = InMemoryCouchDBSourceStore()
    src = _write_codex_fixture(tmp_path)
    result = import_historical_source(
        locator=SourceLocator(provider="codex", source_path=src, capture_metadata_project="neurons"),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.project == "neurons"
    assert result.eligible_for_retirement is True
    assert result.conversation_chunk_count >= 1

    sessions = store.find_by_session(
        session_id_hash=result.session_id_hash, doc_type=dm.SourceDocType.TRANSCRIPT_SESSION
    )
    chunks = store.find_by_session(
        session_id_hash=result.session_id_hash, doc_type=dm.SourceDocType.CONVERSATION_CHUNK
    )
    coverage = store.find_by_session(
        session_id_hash=result.session_id_hash, doc_type=dm.SourceDocType.COVERAGE_MANIFEST
    )
    assert len(sessions) == 1
    assert len(chunks) == result.conversation_chunk_count
    assert len(coverage) == 1
    cov = coverage[0]
    assert cov["conversation_chunk_count"] == result.conversation_chunk_count
    assert cov["project_authority"]["project"] == "neurons"
    assert cov["project_authority"]["eligible_for_retirement"] is True


def test_import_is_idempotent(tmp_path):
    store = InMemoryCouchDBSourceStore()
    src = _write_codex_fixture(tmp_path)
    loc = SourceLocator(provider="codex", source_path=src, capture_metadata_project="neurons")
    first = import_historical_source(locator=loc, store=store)
    count_after_first = len(store.all_docs())
    import_historical_source(locator=loc, store=store)
    assert len(store.all_docs()) == count_after_first
    # re-import did not churn revisions of the session doc
    session_doc = store.get(dm.session_doc_id(first.session_id_hash))
    assert session_doc["_rev"].startswith("1-")


def _write_antigravity_fixture(tmp_path, *, session_id="ag-1"):
    payload = {
        "provider": "antigravity",
        "schema_version": "provider_transcript_fixture.v1",
        "session_id": session_id,
        "started_at": "2026-06-17T01:00:00Z",
        "turns": [
            {"role": "user", "text": "run task", "timestamp": "2026-06-17T01:00:01Z"},
            {"role": "assistant", "text": "done", "timestamp": "2026-06-17T01:00:02Z"},
        ],
    }
    path = tmp_path / f"antigravity-{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_antigravity_lane_imports(tmp_path):
    # agy is Antigravity's CLI and is captured as provider=antigravity, so the
    # antigravity lane covers it; there is no separate agy provider.
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(
            provider="antigravity",
            source_path=_write_antigravity_fixture(tmp_path),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.provider == "antigravity"


def test_agy_is_not_a_separate_provider(tmp_path):
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(provider="agy", source_path=str(tmp_path / "agy.json")),
        store=store,
    )
    assert result.status == ImportStatus.UNKNOWN_PROVIDER
    assert store.all_docs() == []


def test_gemini_live_scope_is_violation(tmp_path):
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(provider="gemini", source_path=str(tmp_path / "g.json"), scope="live"),
        store=store,
    )
    assert result.status == ImportStatus.SCOPE_VIOLATION
    assert store.all_docs() == []


def test_unknown_provider_is_rejected(tmp_path):
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(provider="notathing", source_path=str(tmp_path / "x.json")),
        store=store,
    )
    assert result.status == ImportStatus.UNKNOWN_PROVIDER


def test_missing_source_is_unavailable(tmp_path):
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(
            provider="codex",
            source_path=str(tmp_path / "missing.json"),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert result.status == ImportStatus.SOURCE_UNAVAILABLE
    assert "excluded_from_retirement" in result.notes
    assert store.all_docs() == []


def test_local_path_in_conversation_is_redacted_not_blocked(tmp_path):
    # A /Users/ path in conversation text survives redaction.v2 but must be
    # stripped by the stricter store boundary -> imported, body public-safe.
    store = InMemoryCouchDBSourceStore()
    src = _write_codex_fixture(tmp_path, user_text="see " + "/Users/" + "dev/Projects/neurons/x.py")
    result = import_historical_source(
        locator=SourceLocator(provider="codex", source_path=src, capture_metadata_project="neurons"),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    for chunk in store.find_by_session(
        session_id_hash=result.session_id_hash, doc_type=dm.SourceDocType.CONVERSATION_CHUNK
    ):
        assert "/Users/" not in chunk["body"]


def test_ambiguous_project_is_imported_but_excluded(tmp_path):
    store = InMemoryCouchDBSourceStore()
    src = _write_codex_fixture(tmp_path)
    result = import_historical_source(
        locator=SourceLocator(
            provider="codex",
            source_path=src,
            # conflicting non-authoritative signals, no capture metadata
            cwd="/Users/dev/Projects/dendrite",
            workspace_marker="neurons",
        ),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.project_ambiguous is True
    assert result.eligible_for_retirement is False


def test_batch_report_lists_project_mismatches(tmp_path):
    store = InMemoryCouchDBSourceStore()
    a = _write_codex_fixture(tmp_path, session_id="a")
    b = _write_codex_fixture(tmp_path, session_id="b")
    report = import_historical_sources(
        [
            SourceLocator(provider="codex", source_path=a, capture_metadata_project="neurons", ragflow_project_hint="wrong"),
            SourceLocator(provider="codex", source_path=b, capture_metadata_project="neurons", ragflow_project_hint="neurons"),
        ],
        store=store,
    )
    assert report["imported"] == 2
    assert len(report["project_mismatches"]) == 1
    assert report["project_mismatches"][0]["resolved_project"] == "neurons"
    assert len(report["retirement_eligible"]) == 2
