"""Explicit additive import for one corrective current-source revision.

This module is intentionally separate from the compatibility historical import.
It never reuses a legacy source document id: a correction stages a new,
revision-scoped source set and asks the source-revision core to activate only
that exact allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import document_model as dm
from .session_memory_materializer import (
    mark_projection_pending_if_source_changed,
    update_coverage_with_tool_evidence,
)
from .source_revision import (
    ResolvedSourceRevision,
    SourceRevisionResolutionError,
    activate_source_revision,
    build_revision_scoped_source_documents,
    resolve_active_source_revision,
)
from .source_store import CouchDBSourceStore, SourceStoreConflict, SourceStoreError
from .tool_evidence_bundler import build_tool_evidence_bundle_documents
from ..session_memory.transcript_chunking import build_transcript_chunks
from ..session_memory.transcript_model import ToolEvidenceSummaryRecord
from ..session_memory.transcript_parsers import PARSER_VERSION, ParsedTranscript
from ..session_memory.transcript_parsers.providers.codex import (
    _AdmittedCodexActivationSnapshot,
)


CURRENT_SOURCE_IMPORTED = "imported_current_revision"
CURRENT_SOURCE_CHUNKER_VERSION = "transcript-chunker.v1"


@dataclass(frozen=True)
class CorrectiveCurrentSourceImportResult:
    provider: str
    status: str
    session_id_hash: str = ""
    source_document_ids: tuple[str, ...] = field(default_factory=tuple)
    source_hash: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _error_class(exc: ValueError) -> str:
    return str(exc).split(":", 1)[0].strip() or "source_error"


def _build_current_source_documents(
    *,
    snapshot: _AdmittedCodexActivationSnapshot,
) -> list[dict]:
    """Build one source-document set from one detached activation snapshot."""
    parsed = snapshot.parsed_transcript
    chunks = build_transcript_chunks(parsed)
    return [
        dm.build_transcript_session_document(session=parsed.session),
        *(
            dm.build_conversation_chunk_document(
                chunk=chunk,
                source_locator_hash=parsed.session.source_locator_hash,
            )
            for chunk in chunks
        ),
        *build_tool_evidence_bundle_documents(list(snapshot.tool_evidence)),
    ]


def _revision_scoped_documents(
    *,
    documents: list[dict],
    source_snapshot_hash: str,
) -> list[dict]:
    return build_revision_scoped_source_documents(
        documents=documents,
        source_snapshot_hash=source_snapshot_hash,
        scope_kind="current",
    )


def _safe_provenance(
    *,
    store: CouchDBSourceStore,
    session_id_hash: str,
    source_snapshot_hash: str,
) -> dict[str, str]:
    provenance = {
        "source_snapshot_hash": source_snapshot_hash,
        "parser_version": PARSER_VERSION,
        "chunker_version": CURRENT_SOURCE_CHUNKER_VERSION,
    }
    previous_pointer = store.get(dm.active_source_revision_pointer_doc_id(session_id_hash))
    previous_manifest_hash = str((previous_pointer or {}).get("manifest_hash") or "")
    previous_manifest = store.get(str((previous_pointer or {}).get("manifest_id") or ""))
    previous_provenance = (previous_manifest or {}).get("provenance")
    if (
        isinstance(previous_provenance, dict)
        and previous_provenance.get("source_snapshot_hash") == source_snapshot_hash
    ):
        # Replaying an already-active immutable snapshot must preserve its
        # original provenance exactly; otherwise the immutable manifest id
        # would collide with a changed payload.
        return {str(key): str(value) for key, value in previous_provenance.items()}
    if previous_manifest_hash:
        dm.assert_hash_like("predecessor_manifest_hash", previous_manifest_hash)
        provenance["predecessor_manifest_hash"] = previous_manifest_hash
    return provenance


def _validated_snapshot(
    snapshot: object,
) -> _AdmittedCodexActivationSnapshot:
    """Accept only the immutable snapshot emitted by Codex admission."""
    if not isinstance(snapshot, _AdmittedCodexActivationSnapshot):
        raise ValueError("corrective_snapshot_invalid")
    parsed = snapshot.parsed_transcript
    if not isinstance(parsed, ParsedTranscript):
        raise ValueError("corrective_snapshot_invalid")
    if not isinstance(snapshot.byte_count, int) or isinstance(snapshot.byte_count, bool) or snapshot.byte_count < 0:
        raise ValueError("corrective_snapshot_invalid")
    try:
        dm.assert_hash_like("source_snapshot_hash", snapshot.raw_sha256)
    except ValueError as exc:
        raise ValueError("corrective_snapshot_invalid") from exc

    session = parsed.session
    if session.provider != "codex":
        raise ValueError("corrective_snapshot_invalid")
    for record in snapshot.tool_evidence:
        if not isinstance(record, ToolEvidenceSummaryRecord) or (
            record.session_id_hash != session.session_id_hash
            or record.provider != session.provider
            or record.project != session.project
        ):
            raise ValueError("corrective_snapshot_invalid")
    return snapshot


def activate_admitted_codex_current_source(
    *,
    snapshot: _AdmittedCodexActivationSnapshot,
    store: CouchDBSourceStore,
) -> CorrectiveCurrentSourceImportResult:
    """Stage and activate one already-admitted Codex source revision.

    ``snapshot`` must come from ``admit_codex_locator_snapshot``.
    This function never opens a locator or delegates to a historical parser.
    """
    try:
        snapshot = _validated_snapshot(snapshot)
        parsed = snapshot.parsed_transcript
        documents = _build_current_source_documents(snapshot=snapshot)
        staged_documents = _revision_scoped_documents(
            documents=documents,
            source_snapshot_hash=snapshot.raw_sha256,
        )
    except (AttributeError, TypeError):
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            notes=("corrective_snapshot_unusable", "no_active_pointer_transition"),
        )
    except (ValueError, dm.SourceRedactionLeak) as exc:
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            notes=(_error_class(exc), "no_active_pointer_transition"),
        )

    session_id_hash = parsed.session.session_id_hash
    source_document_ids = tuple(sorted(str(document["_id"]) for document in staged_documents))
    try:
        active_predecessor: ResolvedSourceRevision = resolve_active_source_revision(
            store=store,
            session_id_hash=session_id_hash,
        )
    except SourceRevisionResolutionError:
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            session_id_hash=session_id_hash,
            notes=("active_source_revision_unresolvable", "no_active_pointer_transition"),
        )
    try:
        for document in staged_documents:
            store.put_if_absent(document)
        activated = activate_source_revision(
            store=store,
            session_id_hash=session_id_hash,
            source_document_ids=source_document_ids,
            provenance=_safe_provenance(
                store=store,
                session_id_hash=session_id_hash,
                source_snapshot_hash=snapshot.raw_sha256,
            ),
            expected_predecessor=active_predecessor,
        )
    except ValueError as exc:
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            session_id_hash=session_id_hash,
            notes=(_error_class(exc), "no_active_pointer_transition"),
        )

    try:
        coverage = update_coverage_with_tool_evidence(
            session_id_hash=session_id_hash,
            store=store,
        )
        current = resolve_active_source_revision(
            store=store,
            session_id_hash=session_id_hash,
        )
        persisted_coverage = store.get(dm.coverage_manifest_doc_id(session_id_hash)) or {}
        if (
            coverage is None
            or current.manifest_id != activated.manifest_id
            or current.source_hash != activated.source_hash
            or str(persisted_coverage.get("source_hash") or "") != current.source_hash
            or str(persisted_coverage.get("active_source_manifest_id") or "")
            != str(current.manifest_id or "")
        ):
            return CorrectiveCurrentSourceImportResult(
                provider="codex",
                status="source_unavailable",
                session_id_hash=session_id_hash,
                notes=(
                    "active_source_revision_coverage_unavailable",
                    "no_current_source_import_acknowledgement",
                ),
            )
        mark_projection_pending_if_source_changed(
            session_id_hash=session_id_hash,
            provider=parsed.session.provider,
            project=parsed.session.project,
            source_hash=activated.source_hash,
            store=store,
            source_changed=activated.source_hash != active_predecessor.source_hash,
        )
        current = resolve_active_source_revision(
            store=store,
            session_id_hash=session_id_hash,
        )
        persisted_coverage = store.get(dm.coverage_manifest_doc_id(session_id_hash)) or {}
        if (
            current.manifest_id != activated.manifest_id
            or current.source_hash != activated.source_hash
            or str(persisted_coverage.get("source_hash") or "") != current.source_hash
            or str(persisted_coverage.get("active_source_manifest_id") or "")
            != str(current.manifest_id or "")
        ):
            return CorrectiveCurrentSourceImportResult(
                provider="codex",
                status="source_unavailable",
                session_id_hash=session_id_hash,
                notes=(
                    "active_source_revision_coverage_unavailable",
                    "no_current_source_import_acknowledgement",
                ),
            )
    except (SourceStoreConflict, SourceStoreError, SourceRevisionResolutionError):
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            session_id_hash=session_id_hash,
            notes=(
                "active_source_revision_coverage_unavailable",
                "no_current_source_import_acknowledgement",
            ),
        )
    except ValueError as exc:
        return CorrectiveCurrentSourceImportResult(
            provider="codex",
            status="source_unavailable",
            session_id_hash=session_id_hash,
            notes=(_error_class(exc), "no_active_pointer_transition"),
        )

    return CorrectiveCurrentSourceImportResult(
        provider="codex",
        status=CURRENT_SOURCE_IMPORTED,
        session_id_hash=session_id_hash,
        source_document_ids=source_document_ids,
        source_hash=activated.source_hash,
    )


__all__ = [
    "CURRENT_SOURCE_IMPORTED",
    "CURRENT_SOURCE_CHUNKER_VERSION",
    "CorrectiveCurrentSourceImportResult",
    "activate_admitted_codex_current_source",
]
