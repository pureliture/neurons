from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..redaction import redact_text_v2
from ..document_envelope import (
    DOCUMENT_SCHEMA_VERSION,
    LEDGER_CONTRACT_VERSION,
    ConversationEnvelopeInput,
    build_agent_id,
    build_conversation_document_metadata,
    build_document_filename,
    render_markdown_document,
    _hash_fragment,
)
from .transcript_model import (
    MAX_PACKED_TRANSCRIPT_BODY_CHARS,
    MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS,
    MAX_TRANSCRIPT_TURN_TEXT_CHARS,
    REDACTION_VERSION,
    TOOL_EVIDENCE_CATEGORIES,
    TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
    ToolEvidenceSummaryRecord,
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
    bound_text,
    redact_and_bound_text,
)

TOOL_EVIDENCE_PACKER_VERSION = "tool-evidence-summary-packer.v1"
TOOL_EVIDENCE_DRY_RUN_SCHEMA_VERSION = "agent_knowledge_tool_evidence_dry_run.v1"
_TOOL_EVIDENCE_HEADER_BUDGET_CHARS = 512


@dataclass(frozen=True)
class PackedTranscriptDocument:
    kind: str
    title: str
    body: str
    metadata: dict
    filename: str = ""


def pack_conversation_chunk_document(
    *,
    session: TranscriptSession,
    turns: list[TranscriptTurn],
    tool_events: list[TranscriptToolEvent] | None = None,
    chunk_id: str,
    knowledge_id: str = "",
    capture_request_id: str = "",
    chunk_redacted_text: str = "",
    part_index: int = 1,
    part_count: int = 1,
    char_start: int = 0,
    char_end: int = 0,
) -> PackedTranscriptDocument:
    tool_events = tool_events or []
    if not turns:
        raise ValueError("conversation chunk requires at least one turn")

    turn_start = min(turn.turn_index for turn in turns)
    turn_end = max(turn.turn_index for turn in turns)
    ordered_turns = sorted(turns, key=lambda item: item.turn_index)
    observed_at_start = ordered_turns[0].observed_at or session.started_at
    observed_at_end = ordered_turns[-1].observed_at or session.ended_at or observed_at_start
    content_lines = [
        "# Conversation Chunk",
        "",
        "## Context",
        "",
        f"- provider: {session.provider}",
        f"- project: {session.project}",
        f"- session_id_hash: {session.session_id_hash}",
        f"- turn_range: {turn_start}-{turn_end}",
        "- currentness: historical_conversation_memory",
        "",
        "## Turns",
    ]
    if chunk_redacted_text:
        content_lines.extend(["", "### Chunk Text", redact_text_v2(chunk_redacted_text)])
    else:
        for turn in ordered_turns:
            content_lines.extend(
                [
                    "",
                    f"### Turn {turn.turn_index} {turn.role}",
                    redact_and_bound_text(turn.redacted_text, MAX_TRANSCRIPT_TURN_TEXT_CHARS),
                ]
            )

    if tool_events:
        content_lines.extend(["", "## Tool Events"])
        for event in sorted(tool_events, key=lambda item: item.event_index):
            content_lines.extend(
                [
                    "",
                    f"### Tool Event {event.event_index} {event.tool_name}",
                    redact_and_bound_text(event.redacted_summary, MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS),
                ]
            )

    content_body = bound_text("\n".join(content_lines) + "\n", MAX_PACKED_TRANSCRIPT_BODY_CHARS)
    metadata = build_conversation_document_metadata(
        ConversationEnvelopeInput(
            result_type="conversation_chunk",
            knowledge_id=knowledge_id,
            provider=session.provider,
            project=session.project,
            agent_id=build_agent_id(provider=session.provider, producer="transcript-capture"),
            session_id_hash=session.session_id_hash,
            source_locator_hash=session.source_locator_hash,
            chunk_id=chunk_id,
            turn_start_index=turn_start,
            turn_end_index=turn_end,
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
            privacy_level="private",
            redaction_version=REDACTION_VERSION,
            parser_version="provider-transcript-parser.v1",
            source_status=session.source_status,
            capture_request_id=capture_request_id,
            part_index=part_index,
            part_count=part_count,
            char_start=char_start,
            char_end=char_end,
        )
    )
    body = render_markdown_document(metadata, content_body.splitlines(), max_chars=MAX_PACKED_TRANSCRIPT_BODY_CHARS)
    filename = build_document_filename(
        kind="conversation_chunk",
        provider=session.provider,
        project=session.project,
        session_id_hash=session.session_id_hash,
        turn_start_index=turn_start,
        turn_end_index=turn_end,
        observed_at_start=observed_at_start,
        content=content_body,
    )
    return PackedTranscriptDocument(
        kind="conversation_chunk",
        title=f"{session.provider} conversation chunk {turn_start}-{turn_end}",
        body=body,
        metadata=metadata,
        filename=filename,
    )


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_section_lines(record: ToolEvidenceSummaryRecord) -> list[str]:
    return [
        "",
        f"### {record.evidence_index} {record.category}/{record.outcome}",
        f"- tool: {record.tool_name}",
        f"- command: {record.command_summary}",
        f"- result: {record.redacted_summary}",
    ]


def chunk_tool_evidence_records(
    records: list[ToolEvidenceSummaryRecord],
    *,
    max_chars: int = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
) -> list[list[ToolEvidenceSummaryRecord]]:
    """Split evidence records into ordered parts that each fit one bounded body.

    Order is preserved and no record is dropped or duplicated.
    """
    ordered = list(records)
    if not ordered:
        return []
    parts: list[list[ToolEvidenceSummaryRecord]] = []
    current: list[ToolEvidenceSummaryRecord] = []
    current_len = _TOOL_EVIDENCE_HEADER_BUDGET_CHARS
    for record in ordered:
        section_len = len("\n".join(_evidence_section_lines(record))) + 1
        if current and current_len + section_len > max_chars:
            parts.append(current)
            current = []
            current_len = _TOOL_EVIDENCE_HEADER_BUDGET_CHARS
        current.append(record)
        current_len += section_len
    if current:
        parts.append(current)
    return parts


def _tool_evidence_metadata(
    *,
    session: TranscriptSession,
    records: list[ToolEvidenceSummaryRecord],
    knowledge_id: str,
    part_index: int,
    part_count: int,
    observed_at_start: str,
    observed_at_end: str,
    evidence_index_start: int,
    evidence_index_end: int,
) -> dict:
    categories = ",".join(sorted({record.category for record in records}))
    outcomes = ",".join(sorted({record.outcome for record in records}))
    content_manifest_hash = _sha256("\n".join(sorted(record.content_hash for record in records)))
    chunk_id = f"tool_evidence_{_hash_fragment(session.session_id_hash, 16)}_p{part_index:03d}"
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "result_type": TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        "type": TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        "domain": "agent_memory",
        "knowledge_id": knowledge_id,
        "provider": session.provider,
        "project": session.project,
        "agent_id": build_agent_id(provider=session.provider, producer="tool-evidence"),
        "session_id_hash": session.session_id_hash,
        "source_locator_hash": session.source_locator_hash,
        "chunk_id": chunk_id,
        "turn_start_index": evidence_index_start,
        "turn_end_index": evidence_index_end,
        "part_index": part_index,
        "part_count": part_count,
        "evidence_count": len(records),
        "categories": categories,
        "outcomes": outcomes,
        "observed_at_start": observed_at_start,
        "observed_at_end": observed_at_end,
        "privacy_level": "private",
        "redaction_version": REDACTION_VERSION,
        "parser_version": TOOL_EVIDENCE_PACKER_VERSION,
        "source_status": session.source_status,
        "content_manifest_hash": content_manifest_hash,
        "retrieval_tags": ",".join(
            [session.provider, session.project, "transcript-memory", TOOL_EVIDENCE_SUMMARY_RECORD_TYPE]
        ),
        "ledger_contract": LEDGER_CONTRACT_VERSION,
        "retention_policy": "private_indefinite_until_disabled",
        "currentness": "historical_tool_evidence_memory",
    }


def pack_tool_evidence_summary_document(
    *,
    session: TranscriptSession,
    records: list[ToolEvidenceSummaryRecord],
    knowledge_id: str = "",
    part_index: int = 1,
    part_count: int = 1,
) -> PackedTranscriptDocument:
    """Pack one bounded, redacted tool_evidence_summary source document.

    Append-only and derived from already-redacted evidence records; the packer
    never reintroduces raw text. Metadata is flat for RetiredIndexBridge filters and carries
    the same session_id_hash as the session's conversation_chunk documents.
    """
    if not records:
        raise ValueError("tool_evidence_summary packer requires at least one evidence record")

    ordered = list(records)
    observed = [record.observed_at for record in ordered if record.observed_at]
    observed_at_start = min(observed) if observed else session.started_at
    observed_at_end = max(observed) if observed else (session.ended_at or observed_at_start)
    evidence_index_start = min(record.evidence_index for record in ordered)
    evidence_index_end = max(record.evidence_index for record in ordered)

    content_lines = [
        "# Tool Evidence Summary",
        "",
        "## Context",
        "",
        f"- provider: {session.provider}",
        f"- project: {session.project}",
        f"- session_id_hash: {session.session_id_hash}",
        f"- evidence_count: {len(ordered)}",
        f"- categories: {','.join(sorted({record.category for record in ordered}))}",
        f"- part: {part_index}/{part_count}",
        "- currentness: historical_tool_evidence_memory",
        "",
        "## Evidence",
    ]
    for record in ordered:
        content_lines.extend(_evidence_section_lines(record))

    content_body = bound_text("\n".join(content_lines) + "\n", MAX_PACKED_TRANSCRIPT_BODY_CHARS)
    metadata = _tool_evidence_metadata(
        session=session,
        records=ordered,
        knowledge_id=knowledge_id,
        part_index=part_index,
        part_count=part_count,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        evidence_index_start=evidence_index_start,
        evidence_index_end=evidence_index_end,
    )
    body = render_markdown_document(metadata, content_body.splitlines(), max_chars=MAX_PACKED_TRANSCRIPT_BODY_CHARS)
    filename = build_document_filename(
        kind=TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        provider=session.provider,
        project=session.project,
        session_id_hash=session.session_id_hash,
        turn_start_index=evidence_index_start,
        turn_end_index=evidence_index_end,
        observed_at_start=observed_at_start,
        content=content_body,
    )
    return PackedTranscriptDocument(
        kind=TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        title=f"{session.provider} tool evidence summary {part_index}/{part_count}",
        body=body,
        metadata=metadata,
        filename=filename,
    )


def pack_tool_evidence_summary_documents(
    *,
    session: TranscriptSession,
    records: list[ToolEvidenceSummaryRecord],
) -> list[PackedTranscriptDocument]:
    """Pack a session's evidence into one or more bounded part documents."""
    parts = chunk_tool_evidence_records(records)
    part_count = len(parts)
    return [
        pack_tool_evidence_summary_document(
            session=session,
            records=part,
            part_index=index,
            part_count=part_count,
        )
        for index, part in enumerate(parts, start=1)
    ]


def build_tool_evidence_dry_run_manifest(
    *,
    records: list[ToolEvidenceSummaryRecord],
    provider: str,
    project: str,
    source_locator_hash: str,
) -> dict:
    """Build a local-only dry-run manifest for planned tool_evidence_summary docs.

    Never touches the network or RetiredIndexBridge and never echoes raw source paths,
    transcript text, secrets, or raw ids; evidence records are already redacted.
    """
    ordered = list(records)
    parts = chunk_tool_evidence_records(ordered)
    category_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for record in ordered:
        category_counts[record.category] = category_counts.get(record.category, 0) + 1
        outcome_counts[record.outcome] = outcome_counts.get(record.outcome, 0) + 1
    sessions = sorted({record.session_id_hash for record in ordered})
    return {
        "schema_version": TOOL_EVIDENCE_DRY_RUN_SCHEMA_VERSION,
        "mode": "dry_run",
        "provider": provider,
        "project": project,
        "datasetRole": TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        "sourceDatasetRole": "transcript-memory",
        "documentKind": TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
        "session_id_hash": sessions[0] if len(sessions) == 1 else "",
        "sessions_seen": len(sessions),
        "evidence_planned": len(ordered),
        "category_counts": category_counts,
        "outcome_counts": outcome_counts,
        "high_signal_categories_present": sorted(category_counts),
        "categories_absent": [c for c in TOOL_EVIDENCE_CATEGORIES if c not in category_counts],
        "planned_documents": len(parts),
        "planned_document_parts": [
            {
                "part_index": index,
                "evidence_count": len(part),
                "categories": sorted({record.category for record in part}),
            }
            for index, part in enumerate(parts, start=1)
        ],
        "source_locator_hash": source_locator_hash,
        "raw_source_path_printed": False,
        "raw_transcript_text_printed": False,
        "network_used": False,
        "mutation_performed": False,
        "index_write_performed": False,
    }
