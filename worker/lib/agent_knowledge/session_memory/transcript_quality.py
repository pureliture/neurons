from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..redaction import redact_text_v2
from .transcript_chunking import build_transcript_chunks, _knowledge_id_for_chunk
from .transcript_model import (
    MAX_PACKED_TRANSCRIPT_BODY_CHARS,
    MAX_TRANSCRIPT_CHUNK_TEXT_CHARS,
    MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS,
    MAX_TRANSCRIPT_TURN_TEXT_CHARS,
)
from .transcript_packer import pack_conversation_chunk_document
from .transcript_parsers import parse_transcript_source


__all__ = ["audit_transcript_source"]


def audit_transcript_source(provider: str, source: str | Path, *, project: str) -> dict:
    source_path = Path(source)
    source_shape = _inspect_source_shape(provider, source_path)
    source_locator_hash = _sha256_text(str(source_path))
    parsed = parse_transcript_source(
        provider,
        source_path,
        project=project,
        source_locator_hash=source_locator_hash,
    )
    chunks = build_transcript_chunks(parsed)
    turn_by_index = {turn.turn_index: turn for turn in parsed.turns}
    tool_events_by_turn_id = _tool_events_by_turn_id(parsed.tool_events)
    packed_documents = []
    for chunk in chunks:
        turns = [
            turn_by_index[index]
            for index in range(chunk.turn_start_index, chunk.turn_end_index + 1)
            if index in turn_by_index
        ]
        tool_events = [
            event
            for turn in turns
            for event in tool_events_by_turn_id.get(turn.turn_id_hash, [])
        ]
        packed_documents.append(
            pack_conversation_chunk_document(
                session=parsed.session,
                turns=turns,
                tool_events=tool_events,
                chunk_id=chunk.chunk_id,
                knowledge_id=_knowledge_id_for_chunk(chunk),
                capture_request_id="<redacted>",
                chunk_redacted_text=chunk.redacted_text,
                part_index=chunk.part_index,
                part_count=chunk.part_count,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
        )

    source_message_count = source_shape["message_count"]
    parsed_turn_count = len(parsed.turns)
    source_tool_event_count = source_shape["tool_event_count"]
    parsed_tool_event_count = len(parsed.tool_events)
    source_roles = source_shape["roles"]
    parsed_roles = [turn.role for turn in parsed.turns]
    source_content_chars = source_shape["content_chars"]
    parsed_turn_chars = [len(turn.redacted_text) for turn in parsed.turns]
    redaction_changed = _redaction_changed_flags(source_shape["contents"], parsed.turns)

    turn_truncation_marker_count = sum(_has_truncation_marker(turn.redacted_text) for turn in parsed.turns)
    tool_event_truncation_marker_count = sum(
        _has_truncation_marker(event.redacted_summary) for event in parsed.tool_events
    )
    chunk_truncation_marker_count = sum(_has_truncation_marker(chunk.redacted_text) for chunk in chunks)
    packed_markdown_truncation_marker_count = sum(document.body.count("[truncated]") for document in packed_documents)
    packed_markdown_turn_heading_count = sum(document.body.count("### Turn ") for document in packed_documents)
    packed_markdown_chunk_text_section_count = sum(document.body.count("### Chunk Text") for document in packed_documents)
    packed_markdown_tool_event_heading_count = sum(
        document.body.count("### Tool Event ") for document in packed_documents
    )
    split_chunks = [chunk for chunk in chunks if int(getattr(chunk, "part_count", 1)) > 1]

    coverage_manifest = {
        "source_session_count": 1,
        "source_message_count": source_message_count,
        "parsed_turn_count": parsed_turn_count,
        "source_attachment_count": source_shape["attachment_count"],
        "source_tool_event_count": source_tool_event_count,
        "ignored_metadata_record_count": source_shape["ignored_metadata_record_count"],
        "parsed_tool_event_count": parsed_tool_event_count,
        "ledger_transcript_chunk_count": len(chunks),
        "chunk_turn_ranges": [
            {
                "start": chunk.turn_start_index,
                "end": chunk.turn_end_index,
            }
            for chunk in chunks
        ],
        "split_turn_count": len({(chunk.turn_start_index, chunk.turn_end_index) for chunk in split_chunks}),
        "split_chunk_part_count": len(split_chunks),
        "max_turn_part_count": max((int(getattr(chunk, "part_count", 1)) for chunk in chunks), default=0),
        "packed_markdown_turn_heading_count": packed_markdown_turn_heading_count,
        "packed_markdown_chunk_text_section_count": packed_markdown_chunk_text_section_count,
        "packed_markdown_tool_event_heading_count": packed_markdown_tool_event_heading_count,
    }
    loss_report = {
        "source_messages_not_parsed": max(source_message_count - parsed_turn_count, 0),
        "role_sequence_changes": 0 if source_roles == parsed_roles else 1,
        "redaction_changed_turn_count": sum(redaction_changed),
        "turn_truncation_marker_count": turn_truncation_marker_count,
        "tool_event_truncation_marker_count": tool_event_truncation_marker_count,
        "chunk_truncation_marker_count": chunk_truncation_marker_count,
        "packed_markdown_truncation_marker_count": packed_markdown_truncation_marker_count,
        "dropped_attachment_count": source_shape["attachment_count"],
        "dropped_tool_event_count": max(source_tool_event_count - parsed_tool_event_count, 0),
        "unsupported_event_type_count": source_shape["unsupported_event_type_count"],
        "parser_warning_count": len(parsed.parser_warnings),
    }
    round_trip_audit = {
        "source_to_parser": {
            "count_match": source_message_count == parsed_turn_count,
            "role_sequence_match": source_roles == parsed_roles,
            "char_lengths_match_after_redaction": source_content_chars == parsed_turn_chars,
        },
        "parser_to_chunk": {
            "chunk_count": len(chunks),
            "chunk_redacted_text_chars": [len(chunk.redacted_text) for chunk in chunks],
            "content_hashes": [chunk.content_hash for chunk in chunks],
        },
        "parser_to_packed_markdown": {
            "packed_markdown_chars": [len(document.body) for document in packed_documents],
            "packed_markdown_cap_chars": MAX_PACKED_TRANSCRIPT_BODY_CHARS,
            "starts_with_yaml_front_matter": all(document.body.startswith("---\n") for document in packed_documents),
            "all_turns_have_headings": packed_markdown_turn_heading_count == parsed_turn_count,
            "all_chunks_have_chunk_text_sections": packed_markdown_chunk_text_section_count == len(chunks),
            "metadata_turn_ranges": [
                {
                    "start": document.metadata["turn_start_index"],
                    "end": document.metadata["turn_end_index"],
                    "part_index": document.metadata.get("part_index", 1),
                    "part_count": document.metadata.get("part_count", 1),
                }
                for document in packed_documents
            ],
            "metadata_privacy_level": packed_documents[0].metadata["privacy_level"],
            "metadata_source_status": packed_documents[0].metadata["source_status"],
            "metadata_redaction_version": packed_documents[0].metadata["redaction_version"],
            "metadata_parser_version": packed_documents[0].metadata["parser_version"],
        },
    }
    has_loss = any(
        [
            loss_report["source_messages_not_parsed"],
            loss_report["role_sequence_changes"],
            loss_report["turn_truncation_marker_count"],
            loss_report["tool_event_truncation_marker_count"],
            loss_report["chunk_truncation_marker_count"],
            loss_report["packed_markdown_truncation_marker_count"],
            loss_report["dropped_attachment_count"],
            loss_report["dropped_tool_event_count"],
            loss_report["unsupported_event_type_count"],
            loss_report["parser_warning_count"],
        ]
    )

    return {
        "schema_version": "agent_knowledge_transcript_quality_audit.v1",
        "status": "needs_review" if has_loss else "pass_with_boundaries",
        "provider": provider,
        "project": project,
        "source_file_hash": _sha256_file(source_path),
        "source_locator_hash": source_locator_hash,
        "parser_version": round_trip_audit["parser_to_packed_markdown"]["metadata_parser_version"],
        "redaction_version": round_trip_audit["parser_to_packed_markdown"]["metadata_redaction_version"],
        "limits": {
            "turn_text_chars": MAX_TRANSCRIPT_TURN_TEXT_CHARS,
            "tool_event_text_chars": MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS,
            "chunk_text_chars": MAX_TRANSCRIPT_CHUNK_TEXT_CHARS,
            "packed_markdown_chars": MAX_PACKED_TRANSCRIPT_BODY_CHARS,
        },
        "coverage_manifest": coverage_manifest,
        "loss_report": loss_report,
        "round_trip_audit": round_trip_audit,
        "chunk_continuity_check": _chunk_continuity_check(chunks, parsed.turns, parsed.tool_events),
        "gate_f_plus_representative_contract": _gate_f_plus_representative_contract(
            coverage_manifest,
            network_used=False,
            index_write_performed=False,
        ),
        "retrieval_quality_boundary": {
            "status": "not_evaluated_no_network",
            "index_write_performed": False,
            "live_read_only_retrieval_performed": False,
            "content_recall_reverified": False,
            "authorization_smoke_reused": False,
        },
        "decision": _quality_decision(has_loss, coverage_manifest),
        "network_used": False,
        "mutation_performed": False,
        "index_write_performed": False,
        "raw_source_path_printed": False,
        "raw_transcript_text_printed": False,
    }


def _inspect_source_shape(provider: str, source_path: Path) -> dict:
    if source_path.suffix.lower() == ".jsonl":
        return _inspect_jsonl_source(provider, source_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    turns = payload.get("messages") if provider == "claude" else payload.get("turns")
    if not isinstance(turns, list):
        turns = []
    contents = []
    roles = []
    content_chars = []
    attachment_count = 0
    tool_event_count = 0
    unsupported_event_type_count = 0
    for raw_turn in turns:
        if not isinstance(raw_turn, dict):
            unsupported_event_type_count += 1
            continue
        raw_text = raw_turn.get("content") if provider == "claude" else raw_turn.get("text")
        text = str(raw_text or "")
        contents.append(text)
        roles.append(_normalize_manifest_role(raw_turn.get("role")))
        content_chars.append(len(redact_text_v2(text)))
        count, unsupported = _count_list_field(raw_turn, "attachments")
        attachment_count += count
        unsupported_event_type_count += unsupported
        raw_tool_events = raw_turn.get("tool_events") if provider == "claude" else raw_turn.get("tool_calls")
        count, unsupported = _count_optional_list_value(raw_tool_events)
        tool_event_count += count
        unsupported_event_type_count += unsupported
    return {
        "message_count": len(contents),
        "roles": roles,
        "contents": contents,
        "content_chars": content_chars,
        "attachment_count": attachment_count,
        "tool_event_count": tool_event_count,
        "ignored_metadata_record_count": 0,
        "unsupported_event_type_count": unsupported_event_type_count,
    }


def _inspect_jsonl_source(provider: str, source_path: Path) -> dict:
    roles = []
    contents = []
    ignored_metadata_record_count = 0
    unsupported_event_type_count = 0
    for line in source_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        role, text = _extract_jsonl_turn(provider, record)
        if role and text:
            roles.append(role)
            contents.append(text)
        elif _is_ignored_jsonl_metadata_record(provider, record):
            ignored_metadata_record_count += 1
        else:
            unsupported_event_type_count += 1
    return {
        "message_count": len(contents),
        "roles": roles,
        "contents": contents,
        "content_chars": [len(redact_text_v2(text)) for text in contents],
        "attachment_count": 0,
        "tool_event_count": 0,
        "ignored_metadata_record_count": ignored_metadata_record_count,
        "unsupported_event_type_count": unsupported_event_type_count,
    }


def _extract_jsonl_turn(provider: str, record: dict) -> tuple[str, str]:
    if provider == "claude":
        if record.get("type") not in {"user", "assistant"}:
            return "", ""
        message = record.get("message")
        if not isinstance(message, dict):
            return "", ""
        return _normalize_manifest_role(message.get("role")), _extract_text(message.get("content"))
    if provider == "gemini":
        record_type = str(record.get("type") or "")
        if record_type == "user":
            return "user", _extract_text(record.get("content"))
        if record_type in {"gemini", "model", "assistant"}:
            return "assistant", _extract_text(record.get("content"))
        return "", ""
    if provider == "codex":
        if record.get("type") != "response_item":
            return "", ""
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return "", ""
        role = _normalize_manifest_role(payload.get("role"))
        if role not in {"user", "assistant"}:
            return "", ""
        return role, _extract_text(payload.get("content"))
    return "", ""


def _is_ignored_jsonl_metadata_record(provider: str, record: dict) -> bool:
    record_type = str(record.get("type") or "")
    if provider == "claude":
        return record_type in {"summary", "lastPrompt", "system"}
    if provider == "gemini":
        return record_type in {"", "metadata"} or "$set" in record
    if provider == "codex":
        return record_type in {"session_meta", "turn_context"}
    return False


def _extract_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _count_list_field(record: dict, field: str) -> tuple[int, int]:
    return _count_optional_list_value(record.get(field))


def _count_optional_list_value(value) -> tuple[int, int]:
    if value is None:
        return 0, 0
    if isinstance(value, list):
        return len(value), 0
    return 0, 1


def _tool_events_by_turn_id(tool_events) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for event in tool_events:
        grouped.setdefault(event.turn_id_hash, []).append(event)
    return grouped


def _chunk_continuity_check(chunks, turns, tool_events) -> dict:
    ranges = [
        {
            "start": chunk.turn_start_index,
            "end": chunk.turn_end_index,
            "part_index": int(getattr(chunk, "part_index", 1)),
            "part_count": int(getattr(chunk, "part_count", 1)),
        }
        for chunk in chunks
    ]
    expected_turns = sorted({turn.turn_index for turn in turns})
    chunks_by_turn = {
        turn_index: [
            chunk
            for chunk in chunks
            if int(getattr(chunk, "turn_start_index", 0)) <= turn_index <= int(getattr(chunk, "turn_end_index", 0))
        ]
        for turn_index in expected_turns
    }
    gap_count = sum(1 for turn_index in expected_turns if not chunks_by_turn[turn_index])
    overlap_count = 0
    duplicate_turn_count = 0
    split_part_gap_count = 0
    split_part_duplicate_count = 0
    for turn_index, covering_chunks in chunks_by_turn.items():
        if len(covering_chunks) <= 1:
            continue
        split_status = _split_part_status(turn_index, covering_chunks)
        if split_status["is_split"]:
            split_part_gap_count += split_status["gap_count"]
            split_part_duplicate_count += split_status["duplicate_count"]
            continue
        overlap_count += 1
        duplicate_turn_count += len(covering_chunks) - 1
    turn_ids = {turn.turn_id_hash for turn in turns}
    orphan_tool_event_count = sum(1 for event in tool_events if event.turn_id_hash not in turn_ids)
    status = "pass_multi_chunk" if len(chunks) > 1 else "pass_single_chunk"
    if any(
        [
            gap_count,
            overlap_count,
            duplicate_turn_count,
            split_part_gap_count,
            split_part_duplicate_count,
            orphan_tool_event_count,
        ]
    ):
        status = "needs_review"
    return {
        "status": status,
        "gap_count": gap_count,
        "overlap_count": overlap_count,
        "duplicate_turn_count": duplicate_turn_count,
        "split_part_gap_count": split_part_gap_count,
        "split_part_duplicate_count": split_part_duplicate_count,
        "orphan_tool_event_count": orphan_tool_event_count,
        "ranges": ranges,
    }


def _split_part_status(turn_index: int, chunks) -> dict:
    if any(
        int(getattr(chunk, "turn_start_index", 0)) != turn_index
        or int(getattr(chunk, "turn_end_index", 0)) != turn_index
        or int(getattr(chunk, "part_count", 1)) <= 1
        for chunk in chunks
    ):
        return {"is_split": False, "gap_count": 0, "duplicate_count": 0}
    part_counts = {int(getattr(chunk, "part_count", 1)) for chunk in chunks}
    if len(part_counts) != 1:
        return {"is_split": True, "gap_count": 1, "duplicate_count": 0}
    part_count = part_counts.pop()
    part_indexes = [int(getattr(chunk, "part_index", 1)) for chunk in chunks]
    expected_parts = set(range(1, part_count + 1))
    observed_parts = set(part_indexes)
    duplicate_count = len(part_indexes) - len(observed_parts)
    gap_count = len(expected_parts - observed_parts)
    return {"is_split": True, "gap_count": gap_count, "duplicate_count": duplicate_count}


def _gate_f_plus_representative_contract(
    coverage_manifest: dict,
    *,
    network_used: bool,
    index_write_performed: bool,
) -> dict:
    turn_count = int(coverage_manifest["parsed_turn_count"])
    tool_event_count = int(coverage_manifest["parsed_tool_event_count"])
    chunk_count = int(coverage_manifest["ledger_transcript_chunk_count"])
    attachment_count = int(coverage_manifest["source_attachment_count"])
    long_pass = turn_count >= 8
    tool_pass = tool_event_count >= 3
    chunk_pass = chunk_count >= 2
    status = "representative_no_network_pass" if all([long_pass, tool_pass, chunk_pass]) else "not_representative"
    return {
        "status": status,
        "long_session": {
            "status": "pass" if long_pass else "pending",
            "minimum_turn_count": 8,
            "observed_turn_count": turn_count,
        },
        "tool_heavy_transcript": {
            "status": "pass" if tool_pass else "pending",
            "minimum_tool_event_count": 3,
            "observed_tool_event_count": tool_event_count,
        },
        "multi_chunk_transcript": {
            "status": "pass" if chunk_pass else "pending",
            "minimum_chunk_count": 2,
            "observed_chunk_count": chunk_count,
        },
        "attachment_boundary": {
            "status": "pending_loss_review" if attachment_count else "not_applicable_no_attachment_fixture_support",
            "observed_attachment_count": attachment_count,
        },
        "network_used": network_used,
        "index_write_performed": index_write_performed,
    }


def _quality_decision(has_loss: bool, coverage_manifest: dict) -> str:
    contract = _gate_f_plus_representative_contract(
        coverage_manifest,
        network_used=False,
        index_write_performed=False,
    )
    if has_loss:
        return "revise_packing_before_small_backfill"
    if contract["status"] == "representative_no_network_pass":
        return "gate_f_plus_representative_dry_run_ready"
    return "revise_packing_before_small_backfill"


def _redaction_changed_flags(contents: list[str], turns) -> list[bool]:
    flags = []
    for index, turn in enumerate(turns):
        if index >= len(contents):
            break
        flags.append(redact_text_v2(contents[index]) != contents[index])
    return flags


def _normalize_manifest_role(value) -> str:
    role = str(value or "").lower()
    if role in {"model", "gemini"}:
        return "assistant"
    return role


def _has_truncation_marker(text: str) -> bool:
    return "[truncated]" in text


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
