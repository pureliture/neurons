"""Bounded tool-evidence bundling into CouchDB source documents.

A session's tool evidence records are split into bounded bundles (smaller than a
session, larger than an item) using the existing
``chunk_tool_evidence_records`` budget, then written as ``tool_evidence_bundle``
source documents carrying their evidence index range, member content hashes, and
a coverage hash. Records are already public-ingress-redacted
(``redact_and_bound_evidence_text``), so the bundle body is public-safe.
"""

from __future__ import annotations

from .document_model import (
    build_tool_evidence_bundle_document,
    normalize_observed_interval,
)
from .source_store import CouchDBSourceStore, StoredRevision
from ..session_memory.transcript_model import (
    MAX_PACKED_TRANSCRIPT_BODY_CHARS,
    ToolEvidenceSummaryRecord,
)
from ..session_memory.transcript_packer import chunk_tool_evidence_records


def _bundle_body(records: list[ToolEvidenceSummaryRecord]) -> str:
    lines: list[str] = []
    for record in records:
        lines.append(f"### {record.evidence_index} {record.category}/{record.outcome}")
        lines.append(f"- tool: {record.tool_name}")
        lines.append(f"- command: {record.command_summary}")
        lines.append(f"- result: {record.redacted_summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_tool_evidence_bundle_documents(
    records: list[ToolEvidenceSummaryRecord],
    *,
    max_chars: int = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
) -> list[dict]:
    """Build (but do not store) bounded tool_evidence_bundle documents.

    All records must belong to one session (the bundle is within a session).
    """

    ordered = list(records)
    if not ordered:
        return []
    session_ids = {record.session_id_hash for record in ordered}
    if len(session_ids) != 1:
        raise ValueError("tool evidence bundling is per-session; mixed session_id_hash given")

    sized_parts = chunk_tool_evidence_records(ordered, max_chars=max_chars)
    # A bundle is the smallest temporal relevance unit downstream.  Never put
    # records with different (or invalid/missing) event times behind one bounded
    # interval, otherwise one record's terms can borrow another record's date.
    parts: list[list[ToolEvidenceSummaryRecord]] = []
    for sized_part in sized_parts:
        current: list[ToolEvidenceSummaryRecord] = []
        current_interval: tuple[str, str] | None | object = object()
        for record in sized_part:
            interval = normalize_observed_interval(
                str(record.observed_at or ""),
                str(record.observed_at or ""),
            )
            if current and interval != current_interval:
                parts.append(current)
                current = []
            current.append(record)
            current_interval = interval
        if current:
            parts.append(current)
    part_count = len(parts)
    docs: list[dict] = []
    for part_index, part in enumerate(parts, start=1):
        interval = normalize_observed_interval(
            str(part[0].observed_at or ""),
            str(part[0].observed_at or ""),
        )
        observed_at_start, observed_at_end = interval or ("", "")
        docs.append(
            build_tool_evidence_bundle_document(
                session_id_hash=part[0].session_id_hash,
                provider=part[0].provider,
                project=part[0].project,
                part_index=part_index,
                part_count=part_count,
                evidence_index_start=min(record.evidence_index for record in part),
                evidence_index_end=max(record.evidence_index for record in part),
                record_content_hashes=[record.content_hash for record in part],
                body=_bundle_body(part),
                observed_at_start=observed_at_start,
                observed_at_end=observed_at_end,
            )
        )
    return docs


def store_tool_evidence_bundles(
    records: list[ToolEvidenceSummaryRecord],
    *,
    store: CouchDBSourceStore,
    max_chars: int = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
) -> list[StoredRevision]:
    return [store.put(doc) for doc in build_tool_evidence_bundle_documents(records, max_chars=max_chars)]


__all__ = [
    "build_tool_evidence_bundle_documents",
    "store_tool_evidence_bundles",
]
