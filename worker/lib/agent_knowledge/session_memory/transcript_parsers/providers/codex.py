from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import (
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
    ToolEvidenceSummaryRecord,
)
from ..common import (
    LocatorAdmission,
    ParsedTranscript,
    RawSourceFingerprint,
    _consume_admitted_jsonl_fd,
    _extract_message_text,
    _fingerprint_admitted_fd,
    _load_jsonl_source,
    _normalize_role,
    _sha256,
    _source_admission_error,
    validate_locator_admission,
)
from ..evidence import _build_evidence_records, _extract_output_text


_ADMITTED_CODEX_LOCATOR_FIELDS = frozenset({"provider", "runtime_handle", "locator_hash"})


@dataclass(frozen=True)
class AdmittedCodexLocator:
    runtime_handle: Path
    locator_hash: str


@dataclass(frozen=True)
class _AdmittedCodexActivationSnapshot:
    """Deep-immutable payload consumed by corrective activation only."""

    parsed_transcript: ParsedTranscript
    tool_evidence: tuple[ToolEvidenceSummaryRecord, ...]
    raw_sha256: str
    byte_count: int


def _immutable_activation_snapshot(
    *,
    parsed_transcript: ParsedTranscript,
    tool_evidence: tuple[ToolEvidenceSummaryRecord, ...],
    raw_sha256: str,
    byte_count: int,
) -> _AdmittedCodexActivationSnapshot:
    """Detach bounded parser output into the sole corrective-activation input."""
    return _AdmittedCodexActivationSnapshot(
        parsed_transcript=ParsedTranscript(
            session=parsed_transcript.session,
            turns=tuple(parsed_transcript.turns),  # type: ignore[arg-type]
            tool_events=tuple(parsed_transcript.tool_events),  # type: ignore[arg-type]
            parser_warnings=tuple(parsed_transcript.parser_warnings),  # type: ignore[arg-type]
            source_status=parsed_transcript.source_status,
        ),
        tool_evidence=tuple(tool_evidence),
        raw_sha256=raw_sha256,
        byte_count=byte_count,
    )


def validate_admitted_codex_locator_manifest(manifest: Mapping[str, object]) -> AdmittedCodexLocator:
    """Validate the exact three-field private locator manifest for Codex only."""
    if not isinstance(manifest, Mapping) or set(manifest) != _ADMITTED_CODEX_LOCATOR_FIELDS:
        raise _source_admission_error()
    if manifest.get("provider") != "codex":
        raise _source_admission_error()
    runtime_handle = manifest.get("runtime_handle")
    locator_hash = manifest.get("locator_hash")
    if not isinstance(runtime_handle, (str, Path)) or not str(runtime_handle) or "\x00" in str(runtime_handle):
        raise _source_admission_error()
    if not isinstance(locator_hash, str) or not locator_hash:
        raise _source_admission_error()
    return AdmittedCodexLocator(runtime_handle=Path(runtime_handle), locator_hash=locator_hash)


def _parse_codex_turns_from_fd(
    fd: int,
    admission: LocatorAdmission,
    *,
    project: str,
    source_locator_hash: str,
) -> tuple[ParsedTranscript, RawSourceFingerprint]:
    turns: list[TranscriptTurn] = []
    session_id = ""
    started_at = ""
    ended_at = ""

    def consume(record: dict) -> None:
        nonlocal ended_at, session_id, started_at
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            return
        if record.get("type") != "response_item" or payload.get("type") != "message":
            return
        role = _normalize_role(payload.get("role"))
        if role not in {"user", "assistant"}:
            return
        text = _extract_message_text(payload.get("content"))
        if not text:
            return
        observed_at = str(record.get("timestamp") or payload.get("timestamp") or "")
        if not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        index = len(turns) + 1
        session_hash = _sha256(f"codex:{session_id}")
        turn_hash = _sha256(f"{session_hash}:{index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session_hash,
                turn_index=index,
                role=role,
                observed_at=observed_at,
                redacted_text=text,
            )
        )

    fingerprint = _consume_admitted_jsonl_fd(fd, admission, consume)
    if not session_id or not turns:
        raise _source_admission_error()
    session = TranscriptSession(
        session_id_hash=_sha256(f"codex:{session_id}"),
        provider="codex",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        source_status="source_locator_private_spool_only",
        source_locator_hash=source_locator_hash,
    )
    return (
        ParsedTranscript(
            session=session,
            turns=turns,
            tool_events=[],
            parser_warnings=[],
            source_status=session.source_status,
        ),
        fingerprint,
    )


def _extract_codex_tool_evidence_from_fd(
    fd: int,
    admission: LocatorAdmission,
    *,
    project: str,
) -> tuple[tuple[ToolEvidenceSummaryRecord, ...], RawSourceFingerprint]:
    session_id = ""
    outputs_by_call: dict[str, str] = {}
    patch_success_by_call: dict[str, bool] = {}
    pending_calls: deque[dict] = deque()
    raw_items: list[dict] = []

    def raw_item(entry: dict, output: str) -> dict:
        payload = entry["payload"]
        call_id = str(payload.get("call_id") or "")
        tool_name = str(payload.get("name") or payload.get("type") or "unknown")
        observed_at = str(entry["record"].get("timestamp") or payload.get("timestamp") or "")
        if payload.get("type") == "custom_tool_call" and tool_name == "apply_patch":
            succeeded = patch_success_by_call.get(call_id, "Success" in output or not output)
            return {
                "tool_name": "apply_patch",
                "command": "",
                "output": output,
                "is_error": not succeeded,
                "observed_at": observed_at,
            }
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        command = args.get("cmd") or args.get("command") or args.get("input") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return {
            "tool_name": tool_name,
            "command": str(command),
            "output": output,
            "is_error": False,
            "observed_at": observed_at,
        }

    def flush_resolved() -> None:
        while pending_calls:
            entry = pending_calls[0]
            payload = entry["payload"]
            call_id = str(payload.get("call_id") or "")
            is_patch = payload.get("type") == "custom_tool_call" and (
                str(payload.get("name") or payload.get("type") or "unknown") == "apply_patch"
            )
            has_output = call_id in outputs_by_call
            has_patch_result = call_id in patch_success_by_call
            if not has_output or (is_patch and not has_patch_result):
                return
            pending_calls.popleft()
            raw_items.append(raw_item(entry, outputs_by_call.pop(call_id, "")))
            patch_success_by_call.pop(call_id, None)

    def consume(record: dict) -> None:
        nonlocal session_id
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        payload_type = payload.get("type")
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            return
        if payload_type in {"function_call", "custom_tool_call"}:
            pending_calls.append({"record": record, "payload": payload})
            if len(pending_calls) > admission.max_pending_tool_calls:
                raise _source_admission_error()
            flush_resolved()
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                if call_id not in outputs_by_call and len(outputs_by_call) >= admission.max_pending_tool_calls:
                    raise _source_admission_error()
                outputs_by_call[call_id] = _extract_output_text(payload.get("output"))
                flush_resolved()
        elif payload_type == "patch_apply_end":
            call_id = str(payload.get("call_id") or "")
            if call_id:
                if call_id not in patch_success_by_call and len(patch_success_by_call) >= admission.max_pending_tool_calls:
                    raise _source_admission_error()
                patch_success_by_call[call_id] = bool(payload.get("success"))
                flush_resolved()

    fingerprint = _consume_admitted_jsonl_fd(fd, admission, consume)
    if not session_id:
        raise _source_admission_error()
    flush_resolved()
    if pending_calls or outputs_by_call or patch_success_by_call:
        raise _source_admission_error()

    return (
        tuple(_build_evidence_records(raw_items, session_hash=_sha256(f"codex:{session_id}"), provider="codex", project=project)),
        fingerprint,
    )


def admit_codex_locator_snapshot(
    locator_manifest: Mapping[str, object],
    admission: LocatorAdmission,
    *,
    project: str,
) -> _AdmittedCodexActivationSnapshot:
    """Return one immutable Codex source snapshot after every bounded gate passes.

    No existing parser caller is redirected here. A future private CLI caller
    must opt into this strict three-field manifest and admission contract.
    """
    locator = validate_admitted_codex_locator_manifest(locator_manifest)
    validate_locator_admission(admission)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(locator.runtime_handle, flags)
    except OSError as exc:
        raise _source_admission_error() from exc
    try:
        pre_fingerprint = _fingerprint_admitted_fd(fd, admission)
        expected_matches_pre = (
            pre_fingerprint.raw_sha256 == admission.expected_raw_sha256
            and pre_fingerprint.byte_count == admission.expected_byte_count
        )
        if not expected_matches_pre:
            raise _source_admission_error()
        parsed_transcript, parse_fingerprint = _parse_codex_turns_from_fd(
            fd, admission, project=project, source_locator_hash=locator.locator_hash
        )
        tool_evidence, evidence_fingerprint = _extract_codex_tool_evidence_from_fd(
            fd, admission, project=project
        )
        post_fingerprint = _fingerprint_admitted_fd(fd, admission)
        if not (
            pre_fingerprint == parse_fingerprint == evidence_fingerprint == post_fingerprint
            and post_fingerprint.raw_sha256 == admission.expected_raw_sha256
            and post_fingerprint.byte_count == admission.expected_byte_count
        ):
            raise _source_admission_error()
        snapshot = _immutable_activation_snapshot(
            parsed_transcript=parsed_transcript,
            tool_evidence=tool_evidence,
            raw_sha256=post_fingerprint.raw_sha256,
            byte_count=post_fingerprint.byte_count,
        )
    except ValueError as exc:
        if str(exc) == "source_admission_failed":
            raise
        raise _source_admission_error() from exc
    finally:
        os.close(fd)
    return snapshot

def _parse_codex_native_jsonl(
    path: Path,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    records = _load_jsonl_source(path)
    turns: list[TranscriptTurn] = []
    session_id = ""
    started_at = ""
    ended_at = ""

    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            continue
        if record.get("type") != "response_item":
            continue
        if payload.get("type") != "message":
            continue
        role = _normalize_role(payload.get("role"))
        if role not in {"user", "assistant"}:
            continue
        text = _extract_message_text(payload.get("content"))
        if not text:
            continue
        observed_at = str(record.get("timestamp") or payload.get("timestamp") or "")
        if not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        index = len(turns) + 1
        session_hash = _sha256(f"codex:{session_id}")
        turn_hash = _sha256(f"{session_hash}:{index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session_hash,
                turn_index=index,
                role=role,
                observed_at=observed_at,
                redacted_text=text,
            )
        )

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")
    if not turns:
        raise ValueError("source_parse_failed: missing transcript turns")

    session = TranscriptSession(
        session_id_hash=_sha256(f"codex:{session_id}"),
        provider="codex",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        source_status="source_locator_private_spool_only",
        source_locator_hash=source_locator_hash,
    )
    return ParsedTranscript(
        session=session,
        turns=turns,
        tool_events=[],
        parser_warnings=[],
        source_status=session.source_status,
    )


def extract_codex_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract redacted high-signal tool evidence from a raw Codex JSONL session.

    Append-only and non-destructive: this only reads the source file and never
    touches existing conversation_chunk output. Records are linked to the same
    ``session_id_hash`` the conversation_chunk parser uses
    (``sha256:codex:<session_id>``).
    """
    records = _load_jsonl_source(Path(source_path))
    session_id = ""
    outputs_by_call: dict[str, str] = {}
    patch_success_by_call: dict[str, bool] = {}
    calls: list[dict] = []

    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            continue
        if payload_type in {"function_call", "custom_tool_call"}:
            calls.append({"record": record, "payload": payload})
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                outputs_by_call[call_id] = _extract_output_text(payload.get("output"))
        elif payload_type == "patch_apply_end":
            call_id = str(payload.get("call_id") or "")
            if call_id:
                patch_success_by_call[call_id] = bool(payload.get("success"))

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    raw_items: list[dict] = []
    for entry in calls:
        payload = entry["payload"]
        call_id = str(payload.get("call_id") or "")
        tool_name = str(payload.get("name") or payload.get("type") or "unknown")
        out = outputs_by_call.get(call_id, "")
        observed_at = str(entry["record"].get("timestamp") or payload.get("timestamp") or "")
        if payload.get("type") == "custom_tool_call" and tool_name == "apply_patch":
            succeeded = patch_success_by_call.get(call_id, "Success" in out or not out)
            raw_items.append({"tool_name": "apply_patch", "command": "", "output": out, "is_error": not succeeded, "observed_at": observed_at})
            continue
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        cmd = args.get("cmd") or args.get("command") or args.get("input") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        raw_items.append({"tool_name": tool_name, "command": str(cmd), "output": out, "is_error": False, "observed_at": observed_at})

    return _build_evidence_records(raw_items, session_hash=_sha256(f"codex:{session_id}"), provider="codex", project=project)
