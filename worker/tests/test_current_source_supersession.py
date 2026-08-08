"""Focused contract tests for additive corrective current-source import."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source import migration_cli
from agent_knowledge.couchdb_source import current_source_supersession
from agent_knowledge.couchdb_source.historical_import import (
    PROVIDER_LANES,
    SourceLocator,
    import_historical_source,
)
from agent_knowledge.couchdb_source.current_source_supersession import (
    CorrectiveCurrentSourceImportResult,
    activate_admitted_codex_current_source,
)
from agent_knowledge.couchdb_source.migration_cli import main as migration_main
from agent_knowledge.couchdb_source.source_revision import (
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.session_memory.transcript_model import (
    TranscriptSession,
    TranscriptTurn,
)
from agent_knowledge.session_memory.transcript_parsers.common import LocatorAdmission, ParsedTranscript
from agent_knowledge.session_memory.transcript_parsers.providers.codex import (
    admit_codex_locator_snapshot,
)


PROJECT = "corrective-import"
SESSION_ID_HASH = dm.build_session_id_hash("codex", "corrective-import-session")


def _parsed_source(text: str) -> ParsedTranscript:
    session = TranscriptSession(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        started_at="2026-08-04T00:00:00Z",
        ended_at="2026-08-04T00:01:00Z",
    )
    turn = TranscriptTurn(
        turn_id_hash=dm.sha256_hash(f"turn:{text}"),
        session_id_hash=SESSION_ID_HASH,
        turn_index=1,
        role="user",
        observed_at="2026-08-04T00:00:30Z",
        redacted_text=text,
    )
    return ParsedTranscript(session=session, turns=[turn])


def _admitted_snapshot(tmp_path, text: str, raw_snapshot: str, *, source=None):
    source = source or tmp_path / f"{dm.sha256_hash(raw_snapshot).removeprefix('sha256:')}.jsonl"
    raw = "\n".join(
        json.dumps(record)
        for record in (
            {"type": "session_meta", "payload": {"id": "corrective-import-session"}},
            {
                "type": "response_item",
                "timestamp": "2026-08-04T00:00:30Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": text}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-04T00:00:45Z",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "tool-call",
                    "arguments": json.dumps({"cmd": "uv run pytest -q"}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-04T00:00:46Z",
                "payload": {"type": "function_call_output", "call_id": "tool-call", "output": "1 passed"},
            },
            {"type": "ignored_source_marker", "payload": {"marker": raw_snapshot}},
        )
    ).encode("utf-8") + b"\n"
    source.write_bytes(raw)
    return admit_codex_locator_snapshot(
        {
            "provider": "codex",
            "runtime_handle": str(source),
            "locator_hash": dm.build_source_locator_hash(str(source)),
        },
        LocatorAdmission(
            expected_raw_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            expected_byte_count=len(raw),
            max_bytes=1024 * 1024,
            max_line_bytes=16 * 1024,
            max_record_count=16,
            max_pending_tool_calls=4,
        ),
        project=PROJECT,
    )


def _locator() -> SourceLocator:
    return SourceLocator(provider="codex", source_path="fixture.jsonl", capture_metadata_project=PROJECT)


def _set_codex_parser(monkeypatch, parsed: ParsedTranscript) -> None:
    lane = PROVIDER_LANES["codex"]
    monkeypatch.setitem(PROVIDER_LANES, "codex", replace(lane, parser=lambda *args, **kwargs: parsed))


def _seed_projected_legacy_source(store: InMemoryCouchDBSourceStore, monkeypatch) -> str:
    _set_codex_parser(monkeypatch, _parsed_source("legacy source"))
    legacy = import_historical_source(locator=_locator(), store=store)
    assert legacy.status == "imported"
    resolved = activate_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    state = dm.build_projection_state_document(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        projection_status=dm.ProjectionStatus.PROJECTED,
        active_content_hash=dm.sha256_hash("legacy projection"),
        source_hash=resolved.source_hash,
        projected_source_hash=resolved.source_hash,
    )
    store.put(state)
    return resolved.source_hash


def test_corrective_import_adds_revision_scoped_docs_and_activates_only_allowlist(monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    legacy_source_hash = _seed_projected_legacy_source(store, monkeypatch)
    legacy_documents = store.find_by_session(session_id_hash=SESSION_ID_HASH)
    legacy_ids = {
        document["_id"]
        for document in legacy_documents
        if document["doc_type"] in {
            dm.SourceDocType.TRANSCRIPT_SESSION,
            dm.SourceDocType.CONVERSATION_CHUNK,
        }
    }
    legacy_snapshots = {document_id: store.get(document_id) for document_id in legacy_ids}

    snapshot = _admitted_snapshot(tmp_path, "corrected current source", "verified raw corrective source")
    result = activate_admitted_codex_current_source(snapshot=snapshot, store=store)
    resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))
    coverage = store.get(dm.coverage_manifest_doc_id(SESSION_ID_HASH))

    assert result.status == "imported_current_revision"
    assert set(result.source_document_ids).isdisjoint(legacy_ids)
    assert {
        document["_id"]
        for document in (*resolved.sessions, *resolved.conversation_chunks, *resolved.tool_evidence_bundles)
    } == set(result.source_document_ids)
    assert not any(
        document.get("source_snapshot_schema_version")
        for document in (
            *resolved.sessions,
            *resolved.conversation_chunks,
            *resolved.tool_evidence_bundles,
        )
    )
    assert len(resolved.tool_evidence_bundles) == 1
    assert resolved.tool_evidence_bundles[0]["record_content_hashes"] == [snapshot.tool_evidence[0].content_hash]
    assert {document_id: store.get(document_id) for document_id in legacy_ids} == legacy_snapshots
    assert legacy_source_hash != resolved.source_hash
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash
    assert coverage is not None
    assert coverage["source_hash"] == resolved.source_hash
    assert coverage["active_source_manifest_id"] == resolved.manifest_id
    manifest = store.get(resolved.manifest_id or "")
    source_scopes = {
        document["current_source_scope"]
        for document in (*resolved.sessions, *resolved.conversation_chunks, *resolved.tool_evidence_bundles)
    }
    assert manifest is not None
    assert len(source_scopes) == 1
    assert manifest["provenance"]["source_snapshot_hash"] == snapshot.raw_sha256
    assert set(manifest["provenance"]) == {
        "source_snapshot_hash",
        "parser_version",
        "chunker_version",
        "predecessor_manifest_hash",
    }


def test_corrective_import_is_idempotent_and_changed_verified_snapshot_moves_to_new_revision(monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_projected_legacy_source(store, monkeypatch)

    first_snapshot = _admitted_snapshot(tmp_path, "first corrected source", "first verified raw snapshot")
    first = activate_admitted_codex_current_source(snapshot=first_snapshot, store=store)
    first_resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    pointer_before_duplicate = store.get(dm.active_source_revision_pointer_doc_id(SESSION_ID_HASH))

    duplicate = activate_admitted_codex_current_source(snapshot=first_snapshot, store=store)
    duplicate_resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)
    pointer_after_duplicate = store.get(dm.active_source_revision_pointer_doc_id(SESSION_ID_HASH))

    changed = activate_admitted_codex_current_source(
        snapshot=_admitted_snapshot(tmp_path, "first corrected source", "second verified raw snapshot"),
        store=store,
    )
    changed_resolved = resolve_active_source_revision(store=store, session_id_hash=SESSION_ID_HASH)

    assert duplicate.status == "imported_current_revision"
    assert duplicate.source_document_ids == first.source_document_ids
    assert duplicate_resolved.source_hash == first_resolved.source_hash
    assert pointer_after_duplicate == pointer_before_duplicate
    assert changed.source_document_ids != first.source_document_ids
    assert changed_resolved.source_hash != first_resolved.source_hash
    projection = store.get(dm.projection_state_doc_id(SESSION_ID_HASH))
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == changed_resolved.source_hash


def test_corrective_import_marks_projection_pending_only_when_source_hash_changes(monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_projected_legacy_source(store, monkeypatch)
    source_changed_calls: list[bool] = []

    def record_projection_marker(*, source_changed: bool, **_kwargs) -> None:
        source_changed_calls.append(source_changed)

    monkeypatch.setattr(
        "agent_knowledge.couchdb_source.current_source_supersession.mark_projection_pending_if_source_changed",
        record_projection_marker,
    )
    first_snapshot = _admitted_snapshot(tmp_path, "first corrected source", "first verified raw snapshot")

    first = activate_admitted_codex_current_source(snapshot=first_snapshot, store=store)
    duplicate = activate_admitted_codex_current_source(snapshot=first_snapshot, store=store)
    changed = activate_admitted_codex_current_source(
        snapshot=_admitted_snapshot(tmp_path, "first corrected source", "second verified raw snapshot"),
        store=store,
    )

    assert first.source_hash == duplicate.source_hash
    assert changed.source_hash != first.source_hash
    assert source_changed_calls == [True, False, True]


@pytest.mark.parametrize(
    "coverage_result",
    (
        None,
        {"source_hash": dm.sha256_hash("mismatched corrective coverage")},
    ),
    ids=("missing", "mismatched"),
)
def test_corrective_import_does_not_acknowledge_or_mark_projection_without_coverage_convergence(
    monkeypatch,
    tmp_path,
    coverage_result,
):
    store = InMemoryCouchDBSourceStore()
    _seed_projected_legacy_source(store, monkeypatch)
    projection_calls: list[dict] = []
    monkeypatch.setattr(
        current_source_supersession,
        "update_coverage_with_tool_evidence",
        lambda **_kwargs: coverage_result,
    )
    monkeypatch.setattr(
        current_source_supersession,
        "mark_projection_pending_if_source_changed",
        lambda **kwargs: projection_calls.append(kwargs),
    )

    result = activate_admitted_codex_current_source(
        snapshot=_admitted_snapshot(
            tmp_path,
            "corrected source without converged coverage",
            "verified coverage-gap snapshot",
        ),
        store=store,
    )

    assert result.status == "source_unavailable"
    assert result.source_hash == ""
    assert result.notes == (
        "active_source_revision_coverage_unavailable",
        "no_current_source_import_acknowledgement",
    )
    assert projection_calls == []
    assert resolve_active_source_revision(
        store=store,
        session_id_hash=SESSION_ID_HASH,
    ).is_legacy_unpinned is False


def test_corrective_import_uses_the_admitted_snapshot_without_reopening_locator(monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_projected_legacy_source(store, monkeypatch)
    source = tmp_path / "admitted-then-removed.jsonl"
    snapshot = _admitted_snapshot(
        tmp_path,
        "current source survives locator removal",
        "verified immutable snapshot",
        source=source,
    )
    source.unlink()

    result = activate_admitted_codex_current_source(snapshot=snapshot, store=store)

    assert result.status == "imported_current_revision"
def test_corrective_import_refuses_to_stage_when_existing_active_pointer_cannot_resolve(monkeypatch, tmp_path):
    store = InMemoryCouchDBSourceStore()
    _seed_projected_legacy_source(store, monkeypatch)
    pointer_id = dm.active_source_revision_pointer_doc_id(SESSION_ID_HASH)
    corrupted_pointer = store.get(pointer_id)
    assert corrupted_pointer is not None
    corrupted_pointer["manifest_hash"] = dm.sha256_hash("corrupted active pointer")
    store.put(corrupted_pointer)
    documents_before = copy.deepcopy(store._docs)

    result = activate_admitted_codex_current_source(
        snapshot=_admitted_snapshot(tmp_path, "candidate after corrupt pointer", "verified corrupt pointer source"),
        store=store,
    )

    assert result.status == "source_unavailable"
    assert result.notes == ("active_source_revision_unresolvable", "no_active_pointer_transition")
    assert result.source_document_ids == ()
    assert store._docs == documents_before


def test_corrective_import_cli_requires_explicit_dry_run(capsys):
    rc = migration_main(["--corrective-current-source"])

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report == {
        "dry_run": False,
        "error_class": "corrective_import_requires_dry_run",
        "mutation_performed": False,
        "network_used": False,
        "status": "error",
    }


def test_corrective_import_cli_requires_private_locator_admission_manifest(capsys):
    rc = migration_main(["--corrective-current-source", "--dry-run"])

    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": True,
        "error_class": "corrective_import_requires_locator_admission_manifest",
        "mutation_performed": False,
        "network_used": False,
        "status": "error",
    }


def test_corrective_import_reports_admission_when_activation_raises(monkeypatch):
    admitted_snapshot = object()

    monkeypatch.setattr(
        migration_cli,
        "admit_codex_locator_snapshot",
        lambda *_args, **_kwargs: admitted_snapshot,
    )

    def activation_failure(*_args, **_kwargs):
        raise RuntimeError("activation failed")

    monkeypatch.setattr(
        migration_cli,
        "activate_admitted_codex_current_source",
        activation_failure,
    )

    report = migration_cli.run_corrective_current_import(
        store=InMemoryCouchDBSourceStore(),
        locator_manifest={},
        admission=object(),
        project=PROJECT,
    )

    assert report == {
        "dry_run": True,
        "found": 1,
        "admitted_candidates": 1,
        "imported_current_revisions": 0,
        "errors": 1,
        "mutation_performed": False,
        "network_used": False,
    }


def test_corrective_import_reports_admission_when_activation_is_source_unavailable(monkeypatch):
    admitted_snapshot = object()

    monkeypatch.setattr(
        migration_cli,
        "admit_codex_locator_snapshot",
        lambda *_args, **_kwargs: admitted_snapshot,
    )
    monkeypatch.setattr(
        migration_cli,
        "activate_admitted_codex_current_source",
        lambda *_args, **_kwargs: CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
        ),
    )

    report = migration_cli.run_corrective_current_import(
        store=InMemoryCouchDBSourceStore(),
        locator_manifest={},
        admission=object(),
        project=PROJECT,
    )

    assert report == {
        "dry_run": True,
        "found": 1,
        "admitted_candidates": 1,
        "imported_current_revisions": 0,
        "errors": 1,
        "mutation_performed": False,
        "network_used": False,
    }


def _write_locator_admission_manifest(tmp_path, source, raw: bytes):
    manifest = tmp_path / "private-locator-admission-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "locator": {
                    "provider": "codex",
                    "runtime_handle": str(source),
                    "locator_hash": dm.build_source_locator_hash(str(source)),
                },
                "admission": {
                    "expected_raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "expected_byte_count": len(raw),
                    "max_bytes": max(len(raw), 1),
                    "max_line_bytes": 4096,
                    "max_record_count": 16,
                    "max_pending_tool_calls": 4,
                },
                "project": PROJECT,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_corrective_import_cli_uses_admission_candidate_without_unbounded_parser(tmp_path, capsys, monkeypatch):
    source = tmp_path / "must-not-appear-in-output.jsonl"
    raw = "\n".join(
        json.dumps(record)
        for record in (
            {"type": "session_meta", "payload": {"id": "cli-corrective-session", "cwd": PROJECT}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "safe"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "safe"}]}},
        )
    ) + "\n"
    source.write_text(raw, encoding="utf-8")
    manifest = _write_locator_admission_manifest(tmp_path, source, raw.encode("utf-8"))

    def unbounded_parser_called(*args, **kwargs):
        raise AssertionError("generic historical parser must not run")

    monkeypatch.setattr(
        "agent_knowledge.couchdb_source.migration_cli.import_historical_source",
        unbounded_parser_called,
    )

    rc = migration_main([
        "--corrective-current-source",
        "--dry-run",
        "--locator-admission-manifest",
        str(manifest),
    ])

    output = capsys.readouterr().out
    assert rc == 0
    assert json.loads(output) == {
        "dry_run": True,
        "admitted_candidates": 1,
        "errors": 0,
        "found": 1,
        "imported_current_revisions": 1,
        "mutation_performed": False,
        "network_used": False,
        "status": "ok",
    }
    assert str(source) not in output
    assert str(manifest) not in output
    assert "cli-corrective-session" not in output


def test_corrective_import_cli_rejects_legacy_source_root(capsys):
    rc = migration_main([
        "--corrective-current-source",
        "--dry-run",
        "--source-root",
        "codex=/private/legacy-source-root",
    ])

    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": True,
        "error_class": "corrective_import_rejects_source_root",
        "mutation_performed": False,
        "network_used": False,
        "status": "error",
    }


def test_corrective_import_cli_rejects_unsupported_approval_and_runtime_dir(capsys):
    for flag, value in (
        ("--approval", "/private/approval.json"),
        ("--runtime-dir", "/private/runtime"),
    ):
        rc = migration_main(["--corrective-current-source", "--dry-run", flag, value])

        assert rc == 2
        assert json.loads(capsys.readouterr().out) == {
            "dry_run": True,
            "error_class": "corrective_import_rejects_legacy_target_flags",
            "mutation_performed": False,
            "network_used": False,
            "status": "error",
        }
