from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import patch

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.migration_cli import (
    MIGRATION_PROVIDERS,
    _grok_project_from_path,
    convert_gemini_json_to_fixture,
    default_source_roots,
    enumerate_provider_files,
    extract_cwd,
    main,
    reconcile_coverage,
    run_migration,
    run_tool_evidence,
)
from agent_knowledge.couchdb_source.source_revision import (
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.tool_evidence_bundler import (
    store_tool_evidence_bundles,
)
from agent_knowledge.session_memory.transcript_model import ToolEvidenceSummaryRecord


def _codex_session(root: Path, name: str, cwd: str) -> Path:
    p = root / "2026" / "06" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"timestamp": "2026-06-17T01:00:00Z", "type": "session_meta", "payload": {"id": name, "cwd": cwd}}),
        json.dumps({"timestamp": "2026-06-17T01:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"timestamp": "2026-06-17T01:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_enumerate_codex(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "s1", "/Users/x/Projects/neurons")
    _codex_session(root, "s2", "/Users/x/Projects/neurons")
    assert len(enumerate_provider_files("codex", root)) == 2


def test_extract_cwd_codex(tmp_path):
    p = _codex_session(tmp_path / "codex", "s1", "/Users/x/Projects/neurons")
    assert extract_cwd("codex", p) == "/Users/x/Projects/neurons"


def test_extract_cwd_claude(tmp_path):
    root = tmp_path / "claude" / "proj"
    root.mkdir(parents=True)
    p = root / "sess.jsonl"
    p.write_text(json.dumps({"type": "user", "cwd": "/Users/x/Projects/neurons", "message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8")
    assert extract_cwd("claude", p) == "/Users/x/Projects/neurons"


def test_gemini_json_conversion(tmp_path):
    src = tmp_path / "tmp" / "myproj" / "chats" / "c.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({
        "sessionId": "g1",
        "messages": [
            {"type": "user", "content": [{"text": "question"}], "timestamp": "2026-06-17T01:00:00Z"},
            {"type": "gemini", "content": "answer", "timestamp": "2026-06-17T01:00:05Z"},
        ],
    }), encoding="utf-8")
    out = convert_gemini_json_to_fixture(src, tmp_path / "rt")
    assert out.suffix == ".json" and out.exists()
    fixture = json.loads(out.read_text())
    assert fixture["provider"] == "gemini"
    assert fixture["schema_version"] == "provider_transcript_fixture.v1"
    assert len(fixture["turns"]) == 2


def test_run_tool_evidence_accepts_gemini_json_as_empty_full_generation(tmp_path):
    root = tmp_path / "tmp"
    source = root / "project" / "chats" / "session.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "sessionId": "gemini-empty-evidence",
                "messages": [
                    {"type": "user", "content": "question", "timestamp": "2026-06-17T01:00:00Z"},
                    {"type": "gemini", "content": "answer", "timestamp": "2026-06-17T01:00:01Z"},
                ],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def record_store_call(stored_records, **kwargs):
        captured.update(records=list(stored_records), **kwargs)
        return []

    with patch(
        "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
        side_effect=record_store_call,
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"gemini": root},
            providers=["gemini"],
            runtime_dir=tmp_path / "runtime",
        )

    assert report["errors"] == 0
    assert report["sessions_with_evidence"] == 0
    assert captured["records"] == []
    assert captured["full_session_generation"] is True
    assert captured["session_id_hash"] == dm.build_session_id_hash(
        "gemini", "gemini-empty-evidence"
    )


def test_run_migration_resolves_project_from_cwd(tmp_path):
    # codex session paths are date-based; cwd must drive the project (not ambiguous)
    root = tmp_path / "codex"
    _codex_session(root, "s1", "/Users/x/Projects/neurons")
    _codex_session(root, "s2", "/Users/x/Projects/dendrite")
    store = InMemoryCouchDBSourceStore()
    report = run_migration(store=store, roots={"codex": root}, providers=["codex"], dry_run=True)
    assert report["by_provider"]["codex"]["imported"] == 2
    assert report["ambiguous"] == 0
    # the two sessions resolved to distinct, correct projects
    projects = set()
    for doc in store.all_docs():
        if doc.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST:
            projects.add(doc["project_authority"]["project"])
    assert projects == {"neurons", "dendrite"}


def test_gemini_project_from_tmp_path_segment(tmp_path):
    # gemini transcripts carry no cwd; project must come from ~/.gemini/tmp/<proj>/chats
    chats = tmp_path / "tmp" / "ai-cli-orch-wrapper" / "chats"
    chats.mkdir(parents=True)
    p = chats / "session-x.jsonl"
    p.write_text(json.dumps({"sessionId": "gx", "type": "user", "content": [{"text": "hi"}]}) + "\n", encoding="utf-8")
    store = InMemoryCouchDBSourceStore()
    run_migration(store=store, roots={"gemini": tmp_path / "tmp"}, providers=["gemini"])
    cov = [d for d in store.all_docs() if d.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST]
    assert cov and all(c["project"] == "ai-cli-orch-wrapper" for c in cov)
    assert all(c["project_authority"]["ambiguous"] is False for c in cov)


def test_reconcile_fixes_stale_coverage_count(tmp_path):
    # simulate a multi-file session: chunks accumulate but coverage was overwritten stale
    from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

    store = InMemoryCouchDBSourceStore()
    sid = dm.build_session_id_hash("codex", "multi")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider="codex", project="neurons", started_at="2026-06-17T01:00:00Z")))
    hashes = []
    for i, text in enumerate(("turn one", "turn two", "turn three")):
        ch = TranscriptChunk.from_text(chunk_id=f"chunk_{i}", session_id_hash=sid, provider="codex",
                                       project="neurons", turn_start_index=i, turn_end_index=i, text=text)
        doc = dm.build_conversation_chunk_document(chunk=ch)
        store.put(doc)
        hashes.append(doc["content_hash"])
    # stale coverage: claims only 1 chunk (as if overwritten by the last file)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider="codex", project="neurons",
        conversation_chunk_count=1, tool_evidence_bundle_count=0,
        conversation_content_hashes=hashes[:1], tool_evidence_coverage_hashes=[],
        project_authority={"project": "neurons", "ambiguous": False, "eligible_for_retirement": True}))

    report = reconcile_coverage(store)
    assert report["reconciled"] == 1
    cov = store.get(dm.coverage_manifest_doc_id(sid))
    assert cov["conversation_chunk_count"] == 3  # now matches actual stored chunks
    assert cov["project_authority"]["project"] == "neurons"  # preserved


def test_run_migration_writes_source_families(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "s1", "/Users/x/Projects/neurons")
    store = InMemoryCouchDBSourceStore()
    run_migration(store=store, roots={"codex": root}, providers=["codex"])
    types = {d["doc_type"] for d in store.all_docs()}
    assert dm.SourceDocType.TRANSCRIPT_SESSION in types
    assert dm.SourceDocType.CONVERSATION_CHUNK in types
    assert dm.SourceDocType.COVERAGE_MANIFEST in types


def test_run_tool_evidence_marks_extractor_output_as_full_generation(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "tool-evidence", "/Users/x/Projects/neurons")
    records = [
        ToolEvidenceSummaryRecord(
            session_id_hash=dm.build_session_id_hash("codex", "tool-evidence"),
            provider="codex",
            project="neurons",
            category="test_result",
            outcome="pass",
            tool_name="bash",
            command_summary="uv run pytest -q",
            redacted_summary="focused tests passed",
            evidence_index=0,
        )
    ]
    captured: dict[str, object] = {}

    def record_store_call(
        stored_records,
        *,
        store,
        full_session_generation=False,
        session_id_hash="",
        expected_predecessor=None,
    ):
        captured.update(
            records=stored_records,
            store=store,
            full_session_generation=full_session_generation,
            session_id_hash=session_id_hash,
            expected_predecessor=expected_predecessor,
        )
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=records,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 0
    assert captured["records"] == records
    assert captured["full_session_generation"] is True
    assert captured["session_id_hash"] == records[0].session_id_hash
    assert captured["expected_predecessor"] is not None


def test_run_tool_evidence_groups_same_session_files_before_full_replacement(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "tool-evidence-first", "/Users/x/Projects/neurons")
    second_path = _codex_session(root, "tool-evidence-second", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "shared-tool-evidence")
    records_by_path = {
        str(first_path): ToolEvidenceSummaryRecord(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            category="test_result",
            outcome="pass",
            tool_name="bash",
            command_summary="first command",
            redacted_summary="first result",
            evidence_index=0,
        ),
        str(second_path): ToolEvidenceSummaryRecord(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons",
            category="test_result",
            outcome="pass",
            tool_name="bash",
            command_summary="second command",
            redacted_summary="second result",
            evidence_index=1,
        ),
    }
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def extract_records(_provider, source_path, **_kwargs):
        return [records_by_path[str(source_path)]]

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            side_effect=extract_records,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.parse_transcript_source",
            return_value=SimpleNamespace(
                session=SimpleNamespace(session_id_hash=session_id_hash)
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 0
    assert report["sessions_with_evidence"] == 1
    assert captured == [[records_by_path[str(first_path)], records_by_path[str(second_path)]]]


def test_run_tool_evidence_rejects_extractor_session_identity_drift_before_store(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "source-identity", "/Users/x/Projects/neurons")
    mismatched_record = ToolEvidenceSummaryRecord(
        session_id_hash=dm.build_session_id_hash("codex", "different-session"),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="must not publish",
        redacted_summary="source identity drift",
        evidence_index=0,
    )

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[mismatched_record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=AssertionError("identity drift must not store bundles"),
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0


def test_run_tool_evidence_skips_session_when_late_sibling_appears_before_replace(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "stable-first", "/Users/x/Projects/neurons")
    late_path = _codex_session(root, "late-sibling", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "late-tool-evidence")
    record = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=([first_path], [first_path, late_path]),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.parse_transcript_source",
            return_value=SimpleNamespace(
                session=SimpleNamespace(session_id_hash=session_id_hash)
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_rechecks_new_same_session_sibling_before_full_replacement(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "write-stable-first", "/Users/x/Projects/neurons")
    late_path = _codex_session(root, "write-late-sibling", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "write-late-tool-evidence")
    record = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=([first_path], [first_path], [first_path, late_path]),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli._tool_evidence_source_session_id",
            return_value=session_id_hash,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_skips_when_selected_source_disappears_before_full_replacement(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "write-disappearing-source", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "write-disappearing-source")
    record = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=([first_path], [first_path], []),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli._source_file_fingerprint",
            return_value=(1, 2, 3, 4, 5),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_rechecks_selected_source_fingerprint_before_full_replacement(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "write-changing-source", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "write-changing-source")
    record = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []
    original_fingerprint = (1, 2, 3, 4, 5)
    changed_fingerprint = (1, 2, 4, 5, 6)

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=([first_path], [first_path], [first_path]),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli._source_file_fingerprint",
            side_effect=(
                original_fingerprint,
                original_fingerprint,
                original_fingerprint,
                changed_fingerprint,
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_fails_closed_when_prewrite_sibling_identity_is_unknown(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "write-first", "/Users/x/Projects/neurons")
    second_path = _codex_session(root, "write-second", "/Users/x/Projects/neurons")
    late_path = _codex_session(root, "write-late-unknown", "/Users/x/Projects/neurons")
    first_record = ToolEvidenceSummaryRecord(
        session_id_hash=dm.build_session_id_hash("codex", "write-first"),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    second_record = ToolEvidenceSummaryRecord(
        session_id_hash=dm.build_session_id_hash("codex", "write-second"),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="second command",
        redacted_summary="second result",
        evidence_index=0,
    )
    records_by_path = {
        str(first_path): [first_record],
        str(second_path): [second_record],
    }
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def extract_records(_provider, source_path, **_kwargs):
        return records_by_path[str(source_path)]

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=(
                [first_path, second_path],
                [first_path, second_path],
                [first_path, second_path, late_path],
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            side_effect=extract_records,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli._tool_evidence_source_session_id",
            side_effect=ValueError("source identity unavailable"),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_skips_session_when_source_fingerprint_is_unavailable(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "unreadable-fingerprint", "/Users/x/Projects/neurons")
    record = ToolEvidenceSummaryRecord(
        session_id_hash=dm.build_session_id_hash("codex", "unreadable-fingerprint"),
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[record],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli._source_file_fingerprint",
            return_value=None,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_clears_empty_session_generation(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "empty-tool-evidence", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "empty-tool-evidence")
    captured: dict[str, object] = {}

    def record_store_call(stored_records, **kwargs):
        captured.update(records=list(stored_records), **kwargs)
        return []

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            return_value=[],
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.parse_transcript_source",
            return_value=SimpleNamespace(
                session=SimpleNamespace(session_id_hash=session_id_hash)
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 0
    assert report["sessions_with_evidence"] == 0
    assert captured["records"] == []
    assert captured["session_id_hash"] == session_id_hash
    assert captured["full_session_generation"] is True


def test_run_tool_evidence_skips_incomplete_session_generation(tmp_path):
    root = tmp_path / "codex"
    first_path = _codex_session(root, "complete-first", "/Users/x/Projects/neurons")
    second_path = _codex_session(root, "complete-second", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "incomplete-tool-evidence")
    first_record = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="first command",
        redacted_summary="first result",
        evidence_index=0,
    )
    captured: list[list[ToolEvidenceSummaryRecord]] = []

    def extract_records(_provider, source_path, **_kwargs):
        if str(source_path) == str(first_path):
            return [first_record]
        raise ValueError("simulated sibling extraction failure")

    def record_store_call(stored_records, **_kwargs):
        captured.append(list(stored_records))
        return [object()]

    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
            side_effect=extract_records,
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.parse_transcript_source",
            return_value=SimpleNamespace(
                session=SimpleNamespace(session_id_hash=session_id_hash)
            ),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=record_store_call,
        ),
    ):
        report = run_tool_evidence(
            store=InMemoryCouchDBSourceStore(),
            roots={"codex": root},
            providers=["codex"],
        )

    assert report["errors"] == 1
    assert report["sessions_with_evidence"] == 0
    assert captured == []


def test_run_tool_evidence_rejects_limit_before_source_scan_or_store_mutation(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "limited-first", "/Users/x/Projects/neurons")
    store = InMemoryCouchDBSourceStore()
    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.enumerate_provider_files",
            side_effect=AssertionError("limit rejection must not scan source files"),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.store_tool_evidence_bundles",
            side_effect=AssertionError("limit rejection must not store bundles"),
        ),
    ):
        report = run_tool_evidence(
            store=store,
            roots={"codex": root},
            providers=["codex"],
            limit=1,
        )

    assert report == {
        "by_provider": {},
        "bundles": 0,
        "sessions_with_evidence": 0,
        "errors": 1,
        "error_class": "tool_evidence_limit_unsupported",
    }
    assert store.all_docs() == []


def test_tool_evidence_cli_rejects_limit_before_dry_run_source_seed(capsys):
    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_migration",
            side_effect=AssertionError("limit rejection must not seed source context"),
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_tool_evidence",
            side_effect=AssertionError("limit rejection must not scan or store evidence"),
        ),
    ):
        rc = main(["--tool-evidence", "--dry-run", "--limit", "1"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["status"] == "error"
    assert report["error_class"] == "tool_evidence_limit_unsupported"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_run_tool_evidence_fences_full_generation_to_pre_extraction_predecessor(tmp_path):
    root = tmp_path / "codex"
    _codex_session(root, "overlapping-full-generation", "/Users/x/Projects/neurons")
    session_id_hash = dm.build_session_id_hash("codex", "overlapping-full-generation")
    store = InMemoryCouchDBSourceStore()
    run_migration(store=store, roots={"codex": root}, providers=["codex"])
    captured_predecessor = activate_source_revision(
        store=store,
        session_id_hash=session_id_hash,
    )
    generation_b = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="generation B command",
        redacted_summary="generation B is active",
        evidence_index=0,
    )
    generation_a = ToolEvidenceSummaryRecord(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons",
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="generation A command",
        redacted_summary="generation A must not supersede B",
        evidence_index=0,
    )
    published_b = False

    def extract_after_b_publish(_provider, _source_path, **_kwargs):
        nonlocal published_b
        if not published_b:
            published_b = True
            store_tool_evidence_bundles(
                [generation_b],
                store=store,
                full_session_generation=True,
                session_id_hash=session_id_hash,
            )
        return [generation_a]

    with patch(
        "agent_knowledge.couchdb_source.migration_cli.extract_tool_evidence",
        side_effect=extract_after_b_publish,
    ):
        report = run_tool_evidence(
            store=store,
            roots={"codex": root},
            providers=["codex"],
        )

    current = resolve_active_source_revision(
        store=store,
        session_id_hash=session_id_hash,
    )
    assert captured_predecessor.manifest_id != current.manifest_id
    assert report["errors"] == 1
    assert [bundle["body"] for bundle in current.tool_evidence_bundles] == [
        "### 0 test_result/pass\n- tool: bash\n- command: generation B command\n- result: generation B is active\n"
    ]


def test_tool_evidence_cli_returns_error_when_stability_fence_rejects(capsys):
    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_migration",
            return_value={"errors": 0},
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_tool_evidence",
            return_value={
                "by_provider": {"codex": {"errors": 1}},
                "bundles": 0,
                "sessions_with_evidence": 0,
                "errors": 1,
            },
        ),
    ):
        rc = main(["--tool-evidence", "--dry-run"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "error"
    assert report["errors"] == 1


def test_tool_evidence_dry_run_reports_source_context_seed_errors(capsys):
    with (
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_migration",
            return_value={"errors": 2},
        ),
        patch(
            "agent_knowledge.couchdb_source.migration_cli.run_tool_evidence",
            return_value={
                "by_provider": {"codex": {"errors": 0}},
                "bundles": 0,
                "sessions_with_evidence": 0,
                "errors": 0,
            },
        ),
    ):
        rc = main(["--tool-evidence", "--dry-run"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["source_context_seed_errors"] == 2
    assert report["errors"] == 2
    assert report["status"] == "error"


def test_tool_evidence_dry_run_seeds_source_context_for_full_generation(tmp_path, capsys):
    root = tmp_path / "codex"
    source = _codex_session(root, "tool-evidence-dry-run", "/Users/x/Projects/neurons")
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n".join(
            (
                json.dumps(
                    {
                        "timestamp": "2026-06-17T01:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "dry-run-tool",
                            "arguments": json.dumps({"cmd": "uv run pytest -q"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-06-17T01:00:04Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "dry-run-tool",
                            "output": "1 passed",
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "--tool-evidence",
            "--dry-run",
            "--provider",
            "codex",
            "--source-root",
            f"codex={root}",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "ok"
    assert report["errors"] == 0
    assert report["by_provider"]["codex"]["selected_sessions"] == 1
    assert report["sessions_with_evidence"] == 1
    assert report["bundles"] == 1


def test_transcript_migration_live_run_requires_approval_before_store_setup(capsys):
    rc = main(["--provider", "codex", "--limit", "1"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["schema_version"] == "transcript_migration_cli.v1"
    assert report["error"] == "approval_rejected"
    assert report["reason"] == "approval is required"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def _grok_updates_jsonl(path: Path, *, session_id: str = "gs1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "timestamp": 1_700_000_000,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                },
            },
        }),
        json.dumps({
            "timestamp": 1_700_000_001,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed"},
            },
        }),
        json.dumps({
            "timestamp": 1_700_000_002,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "ok"},
                },
            },
        }),
        json.dumps({
            "timestamp": 1_700_000_003,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed"},
            },
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_migration_providers_includes_grok():
    assert "grok" in MIGRATION_PROVIDERS
    assert "grok" in default_source_roots()


def test_enumerate_and_extract_cwd_grok(tmp_path):
    root = tmp_path / "sessions"
    encoded = quote("/Users/x/Projects/neurons", safe="")
    so_t = root / encoded / "sess-a" / "updates.jsonl"
    _grok_updates_jsonl(so_t, session_id="sess-a")
    # non-SoT jsonl must not be enumerated
    other = root / encoded / "sess-a" / "chat_history.jsonl"
    other.write_text("{}\n", encoding="utf-8")
    # symlink SoT skipped
    link = root / encoded / "sess-b" / "updates.jsonl"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(so_t)

    found = enumerate_provider_files("grok", root)
    assert found == [so_t]
    assert extract_cwd("grok", so_t) == ""
    assert _grok_project_from_path(so_t) == "neurons"
    # Earlier path segment named "sessions" must not steal the layout index.
    nested = (
        tmp_path
        / "sessions"
        / "home"
        / "sessions"
        / quote("/Users/x/Projects/dendrite", safe="")
        / "sess-n"
        / "updates.jsonl"
    )
    _grok_updates_jsonl(nested, session_id="sess-n")
    assert _grok_project_from_path(nested) == "dendrite"


def test_run_migration_grok_project_from_encoded_cwd_not_basename(tmp_path):
    root = tmp_path / "sessions"
    encoded = quote("/Users/x/Projects/neurons", safe="")
    so_t = root / encoded / "sess-a" / "updates.jsonl"
    _grok_updates_jsonl(so_t, session_id="sess-a")
    store = InMemoryCouchDBSourceStore()
    report = run_migration(store=store, roots={"grok": root}, providers=["grok"], dry_run=True)
    assert report["by_provider"]["grok"]["imported"] == 1
    assert report["by_provider"]["grok"]["errors"] == 0
    projects = set()
    for doc in store.all_docs():
        if doc.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST:
            projects.add(doc["project_authority"]["project"])
            assert doc["project_authority"]["project"] != "updates.jsonl"
            assert doc["project_authority"]["ambiguous"] is False
            assert doc["project_authority"]["eligible_for_retirement"] is True
    assert projects == {"neurons"}


def test_run_migration_grok_opaque_group_not_updates_jsonl_project(tmp_path):
    root = tmp_path / "sessions"
    so_t = root / "opaque-slug-abc" / "sess-z" / "updates.jsonl"
    _grok_updates_jsonl(so_t, session_id="sess-z")
    store = InMemoryCouchDBSourceStore()
    report = run_migration(store=store, roots={"grok": root}, providers=["grok"], dry_run=True)
    assert report["by_provider"]["grok"]["imported"] == 1
    for doc in store.all_docs():
        if doc.get("doc_type") == dm.SourceDocType.COVERAGE_MANIFEST:
            assert doc["project_authority"]["project"] != "updates.jsonl"
            assert doc["project_authority"]["ambiguous"] is True
            assert doc["project_authority"]["eligible_for_retirement"] is False
