from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

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
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore


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
