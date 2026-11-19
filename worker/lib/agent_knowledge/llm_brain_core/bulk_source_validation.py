"""Trial-only validated source planner: metadata first, bounded body fetch later."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from agent_knowledge.couchdb_source.document_model import (
    SourceDocType,
    coverage_manifest_doc_id,
    active_source_revision_pointer_doc_id,
)
from agent_knowledge.couchdb_source.source_revision import _parse_pointer, _parse_manifest
from agent_knowledge.couchdb_source.source_revision import resolve_source_hash_for_currentness
from agent_knowledge.couchdb_source.source_revision import resolve_active_source_revision
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid
from agent_knowledge.llm_brain_core.runtime import _conversation_chunk_order_key
_CONVERSATION_FIELDS = [
    "_id", "doc_type", "session_id_hash", "turn_start_index", "turn_end_index",
    "part_index", "char_start", "content_hash", "redaction_version",
    "coverage_hash", "evidence_index_start", "evidence_index_end",
    "record_content_hashes",
]


@dataclass(frozen=True)
class PlannedMember:
    doc_id: str
    lane: str
    ordinal: int
    order_key: tuple
    content_hash: str
    redaction_version: str


@dataclass(frozen=True)
class ValidatedSourcePlan:
    members: tuple[PlannedMember, ...]
    manifest: dict[str, Any]
    source_hash: str


def _unique_keys(keys: Iterable[tuple]) -> tuple[tuple, ...]:
    seen: set[tuple] = set()
    ordered: list[tuple] = []
    for key in keys:
        if key in seen:
            raise SourceInvalid("duplicate member coordinates")
        seen.add(key)
        ordered.append(key)
    return tuple(ordered)


def plan_validated_source(
    source_store: Any, *, session_id_hash: str, expected_source_hash: str
) -> ValidatedSourcePlan:
    """Validate metadata/currentness before any body is read or model called."""
    # Provenance: the caller (product runner) already resolved this hash from
    # the same currentness resolver immediately before planning. Reusing it
    # avoids a full second source scan without weakening start/end fencing.
    current = expected_source_hash
    if not current:
        raise SourceInvalid("missing current source hash from caller")

    manifest = source_store.get(coverage_manifest_doc_id(session_id_hash))
    if not manifest:
        raise SourceInvalid("coverage manifest missing")

    active_docs, _parsed_pointer = iter_active_member_docs(
        source_store, session_id_hash, current
    )
    if active_docs:
        conversation = [
            doc for doc in active_docs
            if str(doc.get("source_doc_type") or "") == SourceDocType.CONVERSATION_CHUNK
        ]
        evidence = [
            doc for doc in active_docs
            if str(doc.get("source_doc_type") or "") == SourceDocType.TOOL_EVIDENCE_BUNDLE
        ]
    else:
        conversation = list(
            source_store.iter_by_session(
                session_id_hash=session_id_hash,
                doc_type=SourceDocType.CONVERSATION_CHUNK,
                fields=_CONVERSATION_FIELDS,
            )
        )
        evidence = list(
            source_store.iter_by_session(
                session_id_hash=session_id_hash,
                doc_type=SourceDocType.TOOL_EVIDENCE_BUNDLE,
                fields=_CONVERSATION_FIELDS,
            )
        )
    _unique_keys(_conversation_chunk_order_key(doc) for doc in conversation)
    conversation.sort(key=_conversation_chunk_order_key)

    declared_chunks = int(manifest.get("conversation_chunk_count") or 0)
    declared_evidence = int(manifest.get("tool_evidence_bundle_count") or 0)
    if len(conversation) != declared_chunks or len(evidence) != declared_evidence:
        raise SourceInvalid("coverage manifest member count mismatch")

    declared_hashes = list(manifest.get("conversation_content_hashes") or [])
    actual_hashes = [str(doc.get("content_hash") or "") for doc in conversation]
    if declared_hashes and actual_hashes != declared_hashes:
        raise SourceInvalid("conversation member hash/order mismatch")
    evidence.sort(key=lambda doc: (int(doc.get("part_index") or 0), str(doc.get("_id") or "")))
    declared_evidence_hashes = list(manifest.get("tool_evidence_coverage_hashes") or [])
    if declared_evidence_hashes:
        actual_evidence_hashes = [
            str(doc.get("coverage_hash") or doc.get("content_hash") or "")
            for doc in evidence
        ]
        if actual_evidence_hashes != declared_evidence_hashes:
            raise SourceInvalid("evidence coverage hash mismatch")

    members: list[PlannedMember] = []
    for lane, docs in (("conversation", conversation), ("evidence", evidence)):
        for ordinal, doc in enumerate(docs):
            redaction = str(doc.get("redaction_version") or "")
            if not redaction:
                raise SourceInvalid("missing redaction provenance")
            members.append(
                PlannedMember(
                    doc_id=str(doc["_id"]),
                    lane=lane,
                    ordinal=ordinal,
                    order_key=_conversation_chunk_order_key(doc) if lane == "conversation" else (),
                    content_hash=str(doc.get("content_hash") or ""),
                    redaction_version=redaction,
                )
            )
    return ValidatedSourcePlan(tuple(members), dict(manifest), current)


_ACTIVE_MEMBER_FIELDS = [
    "_id", "doc_type", "session_id_hash", "source_revision", "member_id",
    "source_document_id", "source_doc_type", "member_hash", "material_hash",
    "redaction_version", "turn_start_index", "turn_end_index", "part_index",
    "char_start", "content_hash", "coverage_hash",
]


def iter_active_member_docs(source_store: Any, session_id_hash: str, expected_source_hash: str):
    """Metadata-only paging over one immutable active member set.

    No member body is fetched here. Callers fetch bodies by doc id one at a time.
    """
    pointer = source_store.get(active_source_revision_pointer_doc_id(session_id_hash))
    if pointer is None:
        return [], None
    parsed = _parse_pointer(pointer, session_id_hash=session_id_hash)
    if parsed["source_hash"] != expected_source_hash:
        raise SourceInvalid("active revision hash disagrees with currentness")
    manifest = source_store.get(parsed["manifest_id"])
    if manifest is None:
        raise SourceInvalid("active revision manifest missing")
    members = _parse_manifest(
        manifest,
        session_id_hash=session_id_hash,
        active_revision=parsed["active_revision"],
        expected_manifest_hash=parsed["manifest_hash"],
        expected_source_hash=parsed["source_hash"],
    )
    allowed = {str(member["member_id"]) for member in members}
    docs = []
    for doc in source_store.iter_by_session(
        session_id_hash=session_id_hash,
        doc_type=SourceDocType.SOURCE_REVISION_MEMBER,
        fields=_ACTIVE_MEMBER_FIELDS,
        page_size=64,
    ):
        if str(doc.get("source_revision") or "") != parsed["active_revision"]:
            continue
        if str(doc.get("member_id") or "") not in allowed:
            continue
        docs.append(doc)
    if len(docs) != len(members):
        raise SourceInvalid("active member count disagrees with manifest")
    expected_hashes = {
        str(member["member_id"]): str(member.get("member_hash") or "")
        for member in members
    }
    for doc in docs:
        expected = expected_hashes.get(str(doc.get("member_id") or ""))
        actual = str(doc.get("member_hash") or "")
        if expected and actual and expected != actual:
            raise SourceInvalid("active member hash mismatch")
    plan_docs = []
    for doc in docs:
        mapped = dict(doc)
        mapped["_id"] = str(doc.get("source_document_id") or doc.get("_id") or "")
        mapped["content_hash"] = str(
            doc.get("material_hash") or doc.get("content_hash") or ""
        )
        plan_docs.append(mapped)
    return plan_docs, parsed
