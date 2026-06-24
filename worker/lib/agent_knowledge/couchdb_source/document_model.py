"""CouchDB transcript-source document families, deterministic ids, and boundaries.

This module is the M1 ownership contract. It defines the six source/evidence
document families, their deterministic ``_id`` and content/coverage hashing, and
the redaction + ownership boundaries that every write must pass. It performs no
I/O; persistence lives in :mod:`.source_store`.

Determinism note: ids and hashes are pure functions of content so a re-import or
re-pack of the same source yields the same ``_id`` and the same content hash,
which is what makes the CouchDB upsert idempotent (design "CouchDB revision
conflict: retry idempotent upsert using deterministic document id and content
hash").
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Mapping

from ..rag_ingress.rag_ready_document import (
    SecretLikeMetadataError,
    assert_no_secret_like_metadata,
)
from ..rag_ingress.server_runtime import public_ingress_leak_violations
from ..redaction import redact_public_ingress_text
from ..session_memory.transcript_model import (
    REDACTION_VERSION,
    ToolEvidenceSummaryRecord,
    TranscriptChunk,
    TranscriptSession,
    canonicalize_project,
)

COUCHDB_SOURCE_SCHEMA_VERSION = "couchdb_transcript_source.v1"
COUCHDB_SOURCE_OWNER = "couchdb-transcript-source"

# RAGFlow keeps only the derived recall surface after cutover. ``transcript-memory``
# is retired: never a CouchDB-source doc type, never a post-cutover projection
# target. These two constants encode the migration's final-state invariant.
RAGFLOW_RECALL_PROFILE = "session-memory"
RETIRED_RAGFLOW_PROFILE = "transcript-memory"


class SourceDocType:
    """The six CouchDB-owned source/evidence document families."""

    TRANSCRIPT_SESSION = "transcript_session"
    CONVERSATION_CHUNK = "conversation_chunk"
    TOOL_EVIDENCE_BUNDLE = "tool_evidence_bundle"
    COVERAGE_MANIFEST = "coverage_manifest"
    PROJECTION_STATE = "projection_state"
    RETENTION_MANIFEST = "retention_manifest"

    _KNOWN = frozenset(
        {
            TRANSCRIPT_SESSION,
            CONVERSATION_CHUNK,
            TOOL_EVIDENCE_BUNDLE,
            COVERAGE_MANIFEST,
            PROJECTION_STATE,
            RETENTION_MANIFEST,
        }
    )

    @classmethod
    def known(cls) -> frozenset[str]:
        return cls._KNOWN


COUCHDB_OWNED_DOC_TYPES = SourceDocType.known()


class ProjectionStatus:
    PENDING = "pending"
    PROJECTED = "projected"
    FAILED = "failed"

    _KNOWN = frozenset({PENDING, PROJECTED, FAILED})

    @classmethod
    def known(cls) -> frozenset[str]:
        return cls._KNOWN


class RetentionTier:
    """Hierarchical retention tiers (design "modified tiered retention")."""

    HOT_FULL = "hot_full"
    HOT_MANIFEST_ONLY = "hot_manifest_only"
    COLD_ARCHIVE_REF = "cold_archive_ref"

    # Ordered hot -> cold; a transition may only move forward (never re-inflate a
    # compacted/archived tier from inside the retention manager).
    _ORDER = (HOT_FULL, HOT_MANIFEST_ONLY, COLD_ARCHIVE_REF)

    @classmethod
    def known(cls) -> frozenset[str]:
        return frozenset(cls._ORDER)

    @classmethod
    def rank(cls, tier: str) -> int:
        return cls._ORDER.index(tier)


class OwnershipViolation(ValueError):
    """Raised when a plane-ownership rule is broken."""


class SourceRedactionLeak(ValueError):
    """Raised when a candidate source body still carries a leak after redaction.

    The message never embeds the raw leaking text -- only the leak category
    names, so the exception itself stays public-safe.
    """


# --- hashing primitives (parity-locked to transcript_model._sha256) -----------


def sha256_hash(value: str) -> str:
    """``"sha256:" + hexdigest`` -- identical scheme to the existing transcript
    pipeline so a CouchDB ``session_id_hash``/``content_hash`` is byte-identical
    to the one the parsers and packer already emit. Parity is asserted by tests.
    """

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_session_id_hash(provider: str, raw_session_id: str) -> str:
    """Canonical session identity hash: ``sha256(f"{provider}:{raw_session_id}")``.

    The raw session id is consumed here and never stored; only the hash leaves
    this function.
    """

    return sha256_hash(f"{provider}:{raw_session_id}")


def build_source_locator_hash(raw_locator: str) -> str:
    """Hash a private source path/locator. The raw locator never leaves here."""

    return sha256_hash(raw_locator)


def build_coverage_hash(content_hashes: Iterable[str]) -> str:
    """Order-independent hash over a set of member content hashes.

    Used as the ``coverage_hash`` for bundles and manifests so coverage can be
    compared without re-reading bodies (design multi-layer coverage gate).
    """

    joined = "\n".join(sorted(str(h) for h in content_hashes))
    return sha256_hash(joined)


def _hash_hex(value: str) -> str:
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


# --- deterministic document ids ----------------------------------------------


def session_doc_id(session_id_hash: str) -> str:
    return f"{SourceDocType.TRANSCRIPT_SESSION}:{_hash_hex(session_id_hash)}"


def conversation_chunk_doc_id(session_id_hash: str, chunk_id: str) -> str:
    # Keyed on the content-addressed chunk_id (not part_index): a session
    # produces many chunks that all share part_index=1, so part_index alone would
    # collide. chunk_id is unique and deterministic, which keeps the upsert
    # idempotent for identical source and distinct for distinct content.
    return f"{SourceDocType.CONVERSATION_CHUNK}:{_hash_hex(session_id_hash)}:{chunk_id}"


def tool_evidence_bundle_doc_id(session_id_hash: str, part_index: int) -> str:
    return f"{SourceDocType.TOOL_EVIDENCE_BUNDLE}:{_hash_hex(session_id_hash)}:p{int(part_index):03d}"


def coverage_manifest_doc_id(session_id_hash: str) -> str:
    return f"{SourceDocType.COVERAGE_MANIFEST}:{_hash_hex(session_id_hash)}"


def projection_state_doc_id(session_id_hash: str) -> str:
    return f"{SourceDocType.PROJECTION_STATE}:{_hash_hex(session_id_hash)}"


def retention_manifest_doc_id(session_id_hash: str) -> str:
    return f"{SourceDocType.RETENTION_MANIFEST}:{_hash_hex(session_id_hash)}"


# --- boundary assertions ------------------------------------------------------


def assert_couchdb_owned(doc_type: str) -> None:
    if doc_type not in COUCHDB_OWNED_DOC_TYPES:
        raise OwnershipViolation(
            f"doc_type not owned by the CouchDB source plane: {doc_type}"
        )


def assert_ragflow_target_allowed(target_profile: str) -> None:
    """The only RAGFlow projection target after cutover is ``session-memory``.

    ``transcript-memory`` is explicitly rejected so a projection_state document
    can never record the retired profile as its destination.
    """

    if target_profile == RETIRED_RAGFLOW_PROFILE:
        raise OwnershipViolation(
            "transcript-memory is retired and is not a valid RAGFlow projection target"
        )
    if target_profile != RAGFLOW_RECALL_PROFILE:
        raise OwnershipViolation(
            f"RAGFlow projection target must be {RAGFLOW_RECALL_PROFILE!r}, got {target_profile!r}"
        )


def assert_source_text_clean(text: str) -> None:
    """Fail-closed redaction boundary: reject any body that still leaks.

    Mirrors the ingress ``public_ingress_leak_violations`` gate so a CouchDB
    write enforces the same contract as a RAGFlow delivery.
    """

    violations = public_ingress_leak_violations(text)
    if violations:
        raise SourceRedactionLeak(
            "source body failed leak check; categories=" + ",".join(sorted(set(violations)))
        )


def assert_hash_like(field: str, value: str) -> None:
    if not value:
        raise ValueError(f"{field} is required")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"{field} must be a 'sha256:<hex>' hash, never a raw value")


# --- document builders --------------------------------------------------------


def _base_document(
    *,
    doc_type: str,
    doc_id: str,
    provider: str,
    project: str,
    session_id_hash: str,
    source_locator_hash: str,
    redaction_version: str,
) -> dict:
    assert_couchdb_owned(doc_type)
    assert_hash_like("session_id_hash", session_id_hash)
    if source_locator_hash:
        assert_hash_like("source_locator_hash", source_locator_hash)
    return {
        "_id": doc_id,
        "doc_type": doc_type,
        "schema_version": COUCHDB_SOURCE_SCHEMA_VERSION,
        "owner": COUCHDB_SOURCE_OWNER,
        "provider": provider,
        "project": canonicalize_project(project),
        "session_id_hash": session_id_hash,
        "source_locator_hash": source_locator_hash,
        "redaction_version": redaction_version,
    }


def _assert_no_secret_like_keys_deep(value: object) -> None:
    """Recursive secret-like-key screen.

    ``assert_no_secret_like_metadata`` only inspects top-level keys; a CouchDB
    source document nests blocks (e.g. ``ledger_comparison``), so a credential
    smuggled into a nested key must be rejected too.
    """

    if isinstance(value, Mapping):
        assert_no_secret_like_metadata(dict(value))
        for item in value.values():
            _assert_no_secret_like_keys_deep(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_secret_like_keys_deep(item)


def _finalize(document: dict) -> dict:
    """Last-line public-safety checks applied to every built document."""

    _assert_no_secret_like_keys_deep(document)
    leaks = public_ingress_leak_violations(json.dumps(document, ensure_ascii=False))
    if leaks:
        raise SourceRedactionLeak(
            "document failed leak check; categories=" + ",".join(sorted(set(leaks)))
        )
    return document


def build_transcript_session_document(*, session: TranscriptSession) -> dict:
    doc = _base_document(
        doc_type=SourceDocType.TRANSCRIPT_SESSION,
        doc_id=session_doc_id(session.session_id_hash),
        provider=session.provider,
        project=session.project,
        session_id_hash=session.session_id_hash,
        source_locator_hash=session.source_locator_hash,
        redaction_version=REDACTION_VERSION,
    )
    doc.update(
        {
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "source_status": session.source_status,
        }
    )
    return _finalize(doc)


def build_conversation_chunk_document(*, chunk: TranscriptChunk, source_locator_hash: str = "") -> dict:
    # TranscriptChunk text is only redaction.v2-redacted, which intentionally
    # keeps local runtime paths for conversation text. The CouchDB source store
    # holds the *public-safe* body (the same stricter pass the ingress worker
    # applies before any delivery), so apply it here and recompute the hash over
    # the stored bytes. The content-addressed chunk_id (over the v2 text) is kept
    # as the stable doc key.
    body = redact_public_ingress_text(chunk.redacted_text)
    assert_source_text_clean(body)
    content_hash = sha256_hash(body)
    doc = _base_document(
        doc_type=SourceDocType.CONVERSATION_CHUNK,
        doc_id=conversation_chunk_doc_id(chunk.session_id_hash, chunk.chunk_id),
        provider=chunk.provider,
        project=chunk.project,
        session_id_hash=chunk.session_id_hash,
        source_locator_hash=source_locator_hash,
        redaction_version=chunk.redaction_version,
    )
    doc.update(
        {
            "chunk_id": chunk.chunk_id,
            "turn_start_index": chunk.turn_start_index,
            "turn_end_index": chunk.turn_end_index,
            "part_index": chunk.part_index,
            "part_count": chunk.part_count,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "content_hash": content_hash,
            "source_status": chunk.source_status,
            "body": body,
        }
    )
    return _finalize(doc)


def build_tool_evidence_bundle_document(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    part_index: int,
    part_count: int,
    evidence_index_start: int,
    evidence_index_end: int,
    record_content_hashes: Iterable[str],
    body: str,
    source_locator_hash: str = "",
    redaction_version: str = REDACTION_VERSION,
) -> dict:
    """A bounded tool-evidence bundle: smaller than a session, larger than an item.

    The bundle records its session linkage, evidence index range, the member
    record content hashes, and a ``coverage_hash`` over them so partial coverage
    can be verified and a single bundle re-processed without re-reading bodies.
    """

    member_hashes = [str(h) for h in record_content_hashes]
    if evidence_index_start > evidence_index_end:
        raise ValueError("evidence_index_start must be <= evidence_index_end")
    assert_source_text_clean(body)
    doc = _base_document(
        doc_type=SourceDocType.TOOL_EVIDENCE_BUNDLE,
        doc_id=tool_evidence_bundle_doc_id(session_id_hash, part_index),
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
        source_locator_hash=source_locator_hash,
        redaction_version=redaction_version,
    )
    doc.update(
        {
            "part_index": part_index,
            "part_count": part_count,
            "evidence_index_start": evidence_index_start,
            "evidence_index_end": evidence_index_end,
            "evidence_count": len(member_hashes),
            "record_content_hashes": member_hashes,
            "coverage_hash": build_coverage_hash(member_hashes),
            "content_hash": sha256_hash(body),
            "body": body,
        }
    )
    return _finalize(doc)


def build_coverage_manifest_document(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    conversation_chunk_count: int,
    tool_evidence_bundle_count: int,
    conversation_content_hashes: Iterable[str],
    tool_evidence_coverage_hashes: Iterable[str],
    source_locator_hash: str = "",
    ledger_comparison: Mapping[str, object] | None = None,
    project_authority: Mapping[str, object] | None = None,
) -> dict:
    """Per-session coverage state consumed by the M5 retirement gate.

    Holds the counts and coverage hashes for conversation chunks and tool
    evidence bundles, an optional comparison block against ledger/ingress and the
    RAGFlow candidate set (comparison only -- RAGFlow project metadata is never
    trusted as authority), and the project-authority resolution (source tier,
    ambiguity, RAGFlow project mismatch) so the retirement gate can exclude
    ambiguous sessions from irreversible retirement proof.
    """

    conv_hashes = [str(h) for h in conversation_content_hashes]
    bundle_hashes = [str(h) for h in tool_evidence_coverage_hashes]
    doc = _base_document(
        doc_type=SourceDocType.COVERAGE_MANIFEST,
        doc_id=coverage_manifest_doc_id(session_id_hash),
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
        source_locator_hash=source_locator_hash,
        redaction_version=REDACTION_VERSION,
    )
    doc.update(
        {
            "conversation_chunk_count": conversation_chunk_count,
            "tool_evidence_bundle_count": tool_evidence_bundle_count,
            "conversation_coverage_hash": build_coverage_hash(conv_hashes),
            "tool_evidence_coverage_hash": build_coverage_hash(bundle_hashes),
            "ledger_comparison": dict(ledger_comparison or {}),
            "project_authority": dict(project_authority or {}),
        }
    )
    return _finalize(doc)


def build_projection_state_document(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    projection_status: str,
    target_profile: str = RAGFLOW_RECALL_PROFILE,
    session_memory_knowledge_id: str = "",
    active_content_hash: str = "",
    failure_reason: str = "",
    source_locator_hash: str = "",
) -> dict:
    """Tracks projection of the derived session-memory to RAGFlow.

    ``target_profile`` is checked against the ownership rule so the retired
    ``transcript-memory`` profile can never be recorded as a projection target.

    ``active_content_hash`` (additive) records the ``content_hash`` of the
    currently-projected session-memory body. It is the join key the Qdrant
    searchable-mirror authority resolver matches against, so a mirror point is
    only authoritative when its content_hash equals the latest projected body's
    hash. Populated on the SUCCESS (PROJECTED) path; left "" on failure paths.
    """

    assert_ragflow_target_allowed(target_profile)
    if projection_status not in ProjectionStatus.known():
        raise ValueError(f"unknown projection_status: {projection_status}")
    doc = _base_document(
        doc_type=SourceDocType.PROJECTION_STATE,
        doc_id=projection_state_doc_id(session_id_hash),
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
        source_locator_hash=source_locator_hash,
        redaction_version=REDACTION_VERSION,
    )
    doc.update(
        {
            "target_profile": target_profile,
            "projection_status": projection_status,
            "session_memory_knowledge_id": session_memory_knowledge_id,
            "active_content_hash": active_content_hash,
            "failure_reason": failure_reason,
        }
    )
    return _finalize(doc)


def build_retention_manifest_document(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    tier: str,
    cold_archive_ref: str = "",
    source_locator_hash: str = "",
) -> dict:
    """Per-session retention tier state (hot full -> hot manifest -> cold ref)."""

    if tier not in RetentionTier.known():
        raise ValueError(f"unknown retention tier: {tier}")
    if tier == RetentionTier.COLD_ARCHIVE_REF and not cold_archive_ref:
        raise ValueError("cold_archive_ref is required for the cold_archive_ref tier")
    doc = _base_document(
        doc_type=SourceDocType.RETENTION_MANIFEST,
        doc_id=retention_manifest_doc_id(session_id_hash),
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
        source_locator_hash=source_locator_hash,
        redaction_version=REDACTION_VERSION,
    )
    doc.update(
        {
            "tier": tier,
            "cold_archive_ref": cold_archive_ref,
        }
    )
    return _finalize(doc)


__all__ = [
    "COUCHDB_SOURCE_SCHEMA_VERSION",
    "COUCHDB_SOURCE_OWNER",
    "COUCHDB_OWNED_DOC_TYPES",
    "RAGFLOW_RECALL_PROFILE",
    "RETIRED_RAGFLOW_PROFILE",
    "SourceDocType",
    "ProjectionStatus",
    "RetentionTier",
    "OwnershipViolation",
    "SourceRedactionLeak",
    "SecretLikeMetadataError",
    "assert_no_secret_like_metadata",
    "assert_couchdb_owned",
    "assert_ragflow_target_allowed",
    "assert_source_text_clean",
    "assert_hash_like",
    "sha256_hash",
    "build_session_id_hash",
    "build_source_locator_hash",
    "build_coverage_hash",
    "session_doc_id",
    "conversation_chunk_doc_id",
    "tool_evidence_bundle_doc_id",
    "coverage_manifest_doc_id",
    "projection_state_doc_id",
    "retention_manifest_doc_id",
    "build_transcript_session_document",
    "build_conversation_chunk_document",
    "build_tool_evidence_bundle_document",
    "build_coverage_manifest_document",
    "build_projection_state_document",
    "build_retention_manifest_document",
    "ToolEvidenceSummaryRecord",
    "TranscriptChunk",
    "TranscriptSession",
]
