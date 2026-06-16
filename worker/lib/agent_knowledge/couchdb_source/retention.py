"""Tiered retention manager for the CouchDB hot source store.

Tiers (design "Retention Manager"; requirement "modified tiered retention"):

    HOT_FULL          recent source kept full in the hot store.
    HOT_MANIFEST_ONLY older source: heavy conversation/tool-evidence bodies
                      dropped from hot; coverage manifest (counts, hashes,
                      session linkage) and a retention manifest kept.
    COLD_ARCHIVE_REF  oldest source: reduced to a cold-archive reference.

Body removal is a destructive, hard-to-reverse operation, so it is gated like a
GC step (AGENTS.md/CLAUDE.md): it only proceeds when the recall surface already
exists (session-memory ``projection_status == projected``), coverage is intact,
and a ``cold_archive_ref`` proves the full redacted source is backed up outside
the hot store. ``plan_retention`` computes the gate; ``apply_retention`` defaults
to ``dry_run=True``. Running it against the live hot store is human-gated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .document_model import (
    ProjectionStatus,
    RetentionTier,
    SourceDocType,
    build_retention_manifest_document,
)
from .source_store import CouchDBSourceStore

# Doc families whose heavy bodies are dropped from the hot store on compaction.
_COMPACTABLE_DOC_TYPES = (SourceDocType.CONVERSATION_CHUNK, SourceDocType.TOOL_EVIDENCE_BUNDLE)


@dataclass(frozen=True)
class RetentionPolicy:
    hot_full_max_age_days: int = 30
    hot_manifest_max_age_days: int = 90  # older than this -> cold archive ref


@dataclass(frozen=True)
class RetentionInput:
    session_id_hash: str
    age_days: int
    projection_status: str = ProjectionStatus.PENDING
    coverage_intact: bool = True
    cold_archive_ref: str = ""


@dataclass(frozen=True)
class RetentionDecision:
    session_id_hash: str
    desired_tier: str  # what age alone suggests
    effective_tier: str  # what the gates allow us to actually do
    allowed: bool  # whether body compaction may proceed
    cold_archive_ref: str
    blocking: tuple[str, ...] = field(default_factory=tuple)


def _desired_tier(age_days: int, policy: RetentionPolicy) -> str:
    if age_days <= policy.hot_full_max_age_days:
        return RetentionTier.HOT_FULL
    if age_days <= policy.hot_manifest_max_age_days:
        return RetentionTier.HOT_MANIFEST_ONLY
    return RetentionTier.COLD_ARCHIVE_REF


def plan_retention(payload: RetentionInput, *, policy: RetentionPolicy = RetentionPolicy()) -> RetentionDecision:
    desired = _desired_tier(payload.age_days, policy)
    if desired == RetentionTier.HOT_FULL:
        return RetentionDecision(
            session_id_hash=payload.session_id_hash,
            desired_tier=desired,
            effective_tier=RetentionTier.HOT_FULL,
            allowed=True,
            cold_archive_ref=payload.cold_archive_ref,
        )

    # Any non-hot tier removes bodies -> apply the GC-style gates.
    blocking: list[str] = []
    if payload.projection_status != ProjectionStatus.PROJECTED:
        blocking.append("session_memory_not_projected")  # recall regression gate
    if not payload.coverage_intact:
        blocking.append("coverage_not_intact")
    if not payload.cold_archive_ref:
        blocking.append("cold_archive_ref_missing")  # backup/rollback evidence

    allowed = not blocking
    effective = desired if allowed else RetentionTier.HOT_FULL
    return RetentionDecision(
        session_id_hash=payload.session_id_hash,
        desired_tier=desired,
        effective_tier=effective,
        allowed=allowed,
        cold_archive_ref=payload.cold_archive_ref,
        blocking=tuple(blocking),
    )


def apply_retention(
    *,
    decision: RetentionDecision,
    store: CouchDBSourceStore,
    dry_run: bool = True,
) -> dict:
    """Apply (or, by default, plan) a retention decision against the hot store.

    Returns the compaction plan. With ``dry_run=True`` (default) nothing is
    mutated. Body removal proceeds only when ``decision.allowed`` and the tier is
    non-hot.
    """

    sid = decision.session_id_hash
    will_compact = decision.allowed and decision.effective_tier != RetentionTier.HOT_FULL

    to_delete: list[str] = []
    if will_compact:
        for doc_type in _COMPACTABLE_DOC_TYPES:
            to_delete.extend(
                d["_id"] for d in store.find_by_session(session_id_hash=sid, doc_type=doc_type)
            )

    result = {
        "session_id_hash": sid,
        "effective_tier": decision.effective_tier,
        "dry_run": dry_run,
        "compacted": False,
        "deleted_doc_ids": to_delete,
        "blocking": list(decision.blocking),
    }
    if dry_run:
        return result

    if will_compact:
        for doc_id in to_delete:
            store.delete(doc_id)
        result["compacted"] = True

    # Always record the retention manifest (even for hot_full) so the tier is
    # auditable. Needs provider/project/source_locator from a surviving doc.
    provider, project, source_locator_hash = _anchor_fields(store, sid)
    manifest = build_retention_manifest_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        tier=decision.effective_tier,
        cold_archive_ref=decision.cold_archive_ref if decision.effective_tier == RetentionTier.COLD_ARCHIVE_REF else "",
        source_locator_hash=source_locator_hash,
    )
    store.put(manifest)
    result["retention_manifest_id"] = manifest["_id"]
    return result


def _anchor_fields(store: CouchDBSourceStore, session_id_hash: str) -> tuple[str, str, str]:
    for doc_type in (
        SourceDocType.TRANSCRIPT_SESSION,
        SourceDocType.COVERAGE_MANIFEST,
        SourceDocType.PROJECTION_STATE,
    ):
        docs = store.find_by_session(session_id_hash=session_id_hash, doc_type=doc_type)
        if docs:
            d = docs[0]
            return d.get("provider", ""), d.get("project", ""), d.get("source_locator_hash", "")
    return "", "", ""


__all__ = [
    "RetentionPolicy",
    "RetentionInput",
    "RetentionDecision",
    "plan_retention",
    "apply_retention",
]
