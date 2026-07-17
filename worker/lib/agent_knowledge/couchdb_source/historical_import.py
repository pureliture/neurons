"""Historical import orchestrator: provider source -> CouchDB source store.

Pipeline (design "Historical Import"):

    enumerate locator -> resolve project (hierarchy authority) -> rebuild
    (parse + redact via the existing provider parsers) -> write
    transcript_session + conversation_chunk + coverage_manifest docs.

Fail-closed throughout: an unreadable/unparseable source, a still-leaking chunk,
or an unsupported provider lane never fabricates coverage -- it returns a status
that excludes the affected coverage from irreversible retirement.

Provider lanes
--------------
Lanes that route to ``parse_transcript_source``: codex, claude, antigravity,
gemini, hermes, grok. ``gemini`` is historical-import-only (no live lane). There
is no separate ``agy`` lane: ``agy`` is Antigravity's headless CLI, and dendrite
captures it as provider ``antigravity`` (``agy_headless_capture`` ->
``normalize_provider_capture_request("antigravity", ...)``), so agy sessions are
imported under the ``antigravity`` lane. A lane registered with ``parser=None``
would be marked ``parser_unavailable`` (and excluded from retirement) rather than
guessing a format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .document_model import (
    SourceRedactionLeak,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_source_revision_token,
    build_source_locator_hash,
    build_transcript_session_document,
)
from .project_authority import ProjectAuthorityInput, resolve_project
from .source_store import CouchDBSourceStore
from ..session_memory.transcript_chunking import build_transcript_chunks
from ..session_memory.transcript_model import canonicalize_provider
from ..session_memory.transcript_parsers import ParsedTranscript, parse_transcript_source


@dataclass(frozen=True)
class ProviderLane:
    provider: str
    live_allowed: bool
    parser: Callable[..., ParsedTranscript] | None


# Single source of truth for which providers are in scope and how they parse.
# Reused by the M4 shadow cutover for the live-allowed flag.
PROVIDER_LANES: dict[str, ProviderLane] = {
    "codex": ProviderLane("codex", live_allowed=True, parser=parse_transcript_source),
    "claude": ProviderLane("claude", live_allowed=True, parser=parse_transcript_source),
    # antigravity covers its headless CLI (agy), which dendrite captures as antigravity.
    "antigravity": ProviderLane("antigravity", live_allowed=True, parser=parse_transcript_source),
    "gemini": ProviderLane("gemini", live_allowed=False, parser=parse_transcript_source),
    # hermes attaches as a first-class provider; it ingests via the generic
    # provider_transcript_fixture.v1 path (no native parser yet).
    "hermes": ProviderLane("hermes", live_allowed=True, parser=parse_transcript_source),
    "grok": ProviderLane("grok", live_allowed=True, parser=parse_transcript_source),
}


class ImportStatus:
    IMPORTED = "imported"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PARSER_UNAVAILABLE = "parser_unavailable"
    LEAK_BLOCKED = "leak_blocked"
    UNKNOWN_PROVIDER = "unknown_provider"
    SCOPE_VIOLATION = "scope_violation"


@dataclass(frozen=True)
class SourceLocator:
    provider: str
    source_path: str
    capture_metadata_project: str = ""
    cwd: str = ""
    workspace_marker: str = ""
    index_project_hint: str = ""
    scope: str = "historical"  # historical | live


@dataclass(frozen=True)
class ImportResult:
    provider: str
    status: str
    session_id_hash: str = ""
    project: str = ""
    project_source: str = ""
    project_ambiguous: bool = False
    index_project_mismatch: bool = False
    eligible_for_retirement: bool = False
    conversation_chunk_count: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def import_historical_source(
    *,
    locator: SourceLocator,
    store: CouchDBSourceStore,
    server_inference: Callable[[], str] | None = None,
    ledger_comparison: Mapping[str, object] | None = None,
) -> ImportResult:
    provider = canonicalize_provider(locator.provider)
    lane = PROVIDER_LANES.get(provider)
    if lane is None:
        return ImportResult(provider=provider, status=ImportStatus.UNKNOWN_PROVIDER, notes=("provider_not_in_migration_scope",))
    if locator.scope == "live" and not lane.live_allowed:
        # Gemini CLI is historical-only; a live event is a scope violation.
        return ImportResult(provider=provider, status=ImportStatus.SCOPE_VIOLATION, notes=("live_scope_not_allowed_for_provider",))
    if lane.parser is None:
        return ImportResult(
            provider=provider,
            status=ImportStatus.PARSER_UNAVAILABLE,
            notes=("parser_not_vendored", "excluded_from_retirement"),
        )

    # Grok ACP SoT files are always named updates.jsonl under ~/.grok/sessions/.
    # Feeding that path into project authority would canonicalize to the basename
    # "updates.jsonl" and falsely mark sessions eligible_for_retirement. Project
    # must come from capture metadata / cwd / workspace marker (or stay unresolved).
    provider_path_for_authority = "" if provider == "grok" else locator.source_path
    resolution = resolve_project(
        ProjectAuthorityInput(
            capture_metadata_project=locator.capture_metadata_project,
            provider_source_path=provider_path_for_authority,
            cwd=locator.cwd,
            workspace_marker=locator.workspace_marker,
            index_project_hint=locator.index_project_hint,
        ),
        server_inference=server_inference,
    )

    source_locator_hash = build_source_locator_hash(locator.source_path)
    try:
        parsed = lane.parser(
            provider,
            locator.source_path,
            project=resolution.project,
            source_locator_hash=source_locator_hash,
        )
    except ValueError as exc:
        return ImportResult(
            provider=provider,
            status=ImportStatus.SOURCE_UNAVAILABLE,
            project=resolution.project,
            project_source=resolution.source,
            project_ambiguous=resolution.ambiguous,
            index_project_mismatch=resolution.index_mismatch,
            notes=(_error_class(exc), "excluded_from_retirement"),
        )

    try:
        chunks = build_transcript_chunks(parsed)
        chunk_docs = [
            build_conversation_chunk_document(chunk=chunk, source_locator_hash=source_locator_hash)
            for chunk in chunks
        ]
    except SourceRedactionLeak as exc:
        # Fail closed: a still-leaking chunk blocks the whole session write.
        return ImportResult(
            provider=provider,
            status=ImportStatus.LEAK_BLOCKED,
            session_id_hash=parsed.session.session_id_hash,
            project=resolution.project,
            project_source=resolution.source,
            project_ambiguous=resolution.ambiguous,
            index_project_mismatch=resolution.index_mismatch,
            notes=(str(exc), "no_projection"),
        )

    session_doc = build_transcript_session_document(session=parsed.session)
    store.put(session_doc)
    for doc in chunk_docs:
        store.put(doc)

    # Coverage is over the *stored* bodies (post public-ingress redaction), so
    # take the recomputed hash from each built document, not the v2 chunk hash.
    conversation_content_hashes = [doc["content_hash"] for doc in chunk_docs]
    coverage_doc = build_coverage_manifest_document(
        session_id_hash=parsed.session.session_id_hash,
        provider=parsed.session.provider,
        project=resolution.project,
        conversation_chunk_count=len(chunks),
        tool_evidence_bundle_count=0,  # M3 fills tool evidence coverage
        conversation_content_hashes=conversation_content_hashes,
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            build_source_revision_token(doc, material_hash_field="content_hash")
            for doc in chunk_docs
        ],
        source_locator_hash=source_locator_hash,
        ledger_comparison=ledger_comparison,
        project_authority=resolution.to_authority_block(),
    )
    store.put(coverage_doc)

    return ImportResult(
        provider=provider,
        status=ImportStatus.IMPORTED,
        session_id_hash=parsed.session.session_id_hash,
        project=resolution.project,
        project_source=resolution.source,
        project_ambiguous=resolution.ambiguous,
        index_project_mismatch=resolution.index_mismatch,
        eligible_for_retirement=resolution.eligible_for_retirement,
        conversation_chunk_count=len(chunks),
        notes=resolution.notes,
    )


def import_historical_sources(
    locators,
    *,
    store: CouchDBSourceStore,
    server_inference: Callable[[], str] | None = None,
) -> dict:
    """Import a batch of locators and return an aggregate report.

    The report includes a project-mismatch list (resolved project vs the polluted
    RetiredIndexBridge project hint) -- the M2 "project mismatch reporting" deliverable.
    """

    results = [
        import_historical_source(locator=loc, store=store, server_inference=server_inference)
        for loc in locators
    ]
    imported = [r for r in results if r.status == ImportStatus.IMPORTED]
    return {
        "total": len(results),
        "imported": len(imported),
        "by_status": _count_by_status(results),
        "project_mismatches": [
            {
                "provider": r.provider,
                "session_id_hash": r.session_id_hash,
                "resolved_project": r.project,
                "project_source": r.project_source,
            }
            for r in results
            if r.index_project_mismatch
        ],
        "ambiguous_sessions": [
            {"provider": r.provider, "session_id_hash": r.session_id_hash}
            for r in results
            if r.project_ambiguous
        ],
        "retirement_eligible": [r.session_id_hash for r in imported if r.eligible_for_retirement],
        "results": results,
    }


def _count_by_status(results) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def _error_class(exc: ValueError) -> str:
    # Parser errors are class-like strings ("source_unreadable",
    # "source_parse_failed: ..."); keep only the leading class token, public-safe.
    return str(exc).split(":", 1)[0].strip() or "source_error"


__all__ = [
    "PROVIDER_LANES",
    "ProviderLane",
    "ImportStatus",
    "SourceLocator",
    "ImportResult",
    "import_historical_source",
    "import_historical_sources",
]
