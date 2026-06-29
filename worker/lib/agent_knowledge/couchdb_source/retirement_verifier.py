"""Multi-layer RetiredIndexBridge transcript-memory retirement verifier.

Retirement of RetiredIndexBridge ``transcript-memory`` is approved only when all three
independent gates pass (design "Coverage And Retirement Verifier"; requirement
"다층 검증" -- a single gate is never sufficient):

1. coverage  -- CouchDB source covers the expected session/chunk/bundle counts
                with matching coverage hashes (no loss vs ledger/ingress).
2. rebuild   -- session-memory rebuilt from CouchDB source is fully materialized
                (no dropped chunk or tool evidence bundle).
3. recall    -- representative recall smoke against session-memory returns the
                expected result.

Ambiguous-project sessions are excluded from the irreversible retirement proof.

Scope: this verifier produces a readiness *report*. It performs no live RetiredIndexBridge
mutation. The actual ``transcript-memory`` disable/delete and the removal of the
runtime transcript-memory write/read callsites are an explicit breaking change
and a hard-to-reverse live operation -- both are human-gated and are NOT
performed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .document_model import (
    SourceDocType,
    build_coverage_hash,
    coverage_manifest_doc_id,
)
from .session_memory_materializer import materialize_session_memory
from .source_store import CouchDBSourceStore


@dataclass(frozen=True)
class SessionExpectation:
    session_id_hash: str
    expected_conversation_chunks: int
    expected_tool_evidence_bundles: int
    index_candidate_count: int = 0  # comparison only; never authority
    recall_smoke_passed: bool | None = None  # None = smoke not run


@dataclass(frozen=True)
class SessionRetirementVerdict:
    session_id_hash: str
    eligible: bool
    coverage_pass: bool
    rebuild_pass: bool
    recall_pass: bool
    ready: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RetirementReadiness:
    ready: bool
    coverage_pass: bool
    rebuild_pass: bool
    recall_pass: bool
    sessions: tuple[SessionRetirementVerdict, ...]
    excluded_sessions: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Always present so a caller cannot mistake the report for a live action.
    live_action_required: str = (
        "live RetiredIndexBridge transcript-memory disable/delete and runtime callsite removal "
        "are human-gated and not performed by this verifier"
    )


def verify_session_retirement(
    *,
    expectation: SessionExpectation,
    store: CouchDBSourceStore,
) -> SessionRetirementVerdict:
    sid = expectation.session_id_hash
    coverage = store.get(coverage_manifest_doc_id(sid))
    chunks = store.find_by_session(session_id_hash=sid, doc_type=SourceDocType.CONVERSATION_CHUNK)
    bundles = store.find_by_session(session_id_hash=sid, doc_type=SourceDocType.TOOL_EVIDENCE_BUNDLE)

    notes: list[str] = []

    eligible = False
    if coverage is not None:
        eligible = bool(coverage.get("project_authority", {}).get("eligible_for_retirement", False))
    if not eligible:
        notes.append("project_ambiguous_or_unresolved")

    # --- gate 1: coverage ---
    coverage_pass = coverage is not None
    if coverage is None:
        notes.append("no_coverage_manifest")
    else:
        if len(chunks) < expectation.expected_conversation_chunks:
            coverage_pass = False
            notes.append("conversation_chunk_shortfall")
        if len(bundles) < expectation.expected_tool_evidence_bundles:
            coverage_pass = False
            notes.append("tool_evidence_bundle_shortfall")
        recomputed = build_coverage_hash([c.get("content_hash", "") for c in chunks])
        if recomputed != coverage.get("conversation_coverage_hash"):
            coverage_pass = False
            notes.append("conversation_coverage_hash_mismatch")

    # --- gate 2: rebuild ---
    materialized = materialize_session_memory(session_id_hash=sid, store=store)
    rebuild_pass = materialized.fully_materialized
    if not rebuild_pass:
        notes.append("rebuild_not_fully_materialized")

    # --- gate 3: recall smoke ---
    recall_pass = expectation.recall_smoke_passed is True
    if expectation.recall_smoke_passed is None:
        notes.append("recall_smoke_not_run")
    elif expectation.recall_smoke_passed is False:
        notes.append("recall_smoke_failed")

    ready = eligible and coverage_pass and rebuild_pass and recall_pass
    return SessionRetirementVerdict(
        session_id_hash=sid,
        eligible=eligible,
        coverage_pass=coverage_pass,
        rebuild_pass=rebuild_pass,
        recall_pass=recall_pass,
        ready=ready,
        notes=tuple(notes),
    )


def verify_retirement(
    *,
    expectations,
    store: CouchDBSourceStore,
) -> RetirementReadiness:
    verdicts = tuple(
        verify_session_retirement(expectation=exp, store=store) for exp in expectations
    )
    eligible = [v for v in verdicts if v.eligible]
    excluded = tuple(v.session_id_hash for v in verdicts if not v.eligible)

    notes: list[str] = []
    if not eligible:
        notes.append("no_eligible_sessions")

    # Each gate must pass across every eligible session; an empty eligible set is
    # not "ready" (nothing was proven).
    coverage_pass = bool(eligible) and all(v.coverage_pass for v in eligible)
    rebuild_pass = bool(eligible) and all(v.rebuild_pass for v in eligible)
    recall_pass = bool(eligible) and all(v.recall_pass for v in eligible)
    ready = coverage_pass and rebuild_pass and recall_pass

    return RetirementReadiness(
        ready=ready,
        coverage_pass=coverage_pass,
        rebuild_pass=rebuild_pass,
        recall_pass=recall_pass,
        sessions=verdicts,
        excluded_sessions=excluded,
        notes=tuple(notes),
    )


__all__ = [
    "SessionExpectation",
    "SessionRetirementVerdict",
    "RetirementReadiness",
    "verify_session_retirement",
    "verify_retirement",
]
