"""Focused TDD coverage for the Codex-only streaming admission seam."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_knowledge.session_memory.transcript_parsers.common import LocatorAdmission
from agent_knowledge.session_memory.transcript_parsers.providers import codex
from agent_knowledge.session_memory.transcript_parsers.providers.codex import (
    admit_codex_locator_snapshot,
    validate_admitted_codex_locator_manifest,
)


PROJECT = "streaming-admission-test"
LOCATOR_HASH = "sha256:" + "a" * 64


def _response(payload: dict, *, timestamp: str = "2026-08-04T00:00:01Z") -> dict:
    return {"type": "response_item", "timestamp": timestamp, "payload": payload}


def _source_records(*, extra_calls: int = 0) -> list[dict]:
    records = [
        {"type": "session_meta", "payload": {"id": "streaming-session"}},
        _response({"type": "message", "role": "user", "content": [{"text": "run focused tests"}]}),
        _response({"type": "message", "role": "assistant", "content": [{"text": "running"}]}),
        _response(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-0",
                "arguments": json.dumps({"cmd": "uv run pytest -q"}),
            }
        ),
        _response({"type": "function_call_output", "call_id": "call-0", "output": "1 passed in 0.01s"}),
        _response({"type": "custom_tool_call", "name": "apply_patch", "call_id": "patch-0", "input": "*** Begin Patch"}),
        _response({"type": "patch_apply_end", "call_id": "patch-0", "success": False}),
        _response({"type": "custom_tool_call_output", "call_id": "patch-0", "output": "patch rejected"}),
    ]
    for index in range(extra_calls):
        records.append(
            _response(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": f"extra-{index}",
                    "arguments": json.dumps({"cmd": "git status --short"}),
                }
            )
        )
    return records


def _write_source(path: Path, records: list[dict] | None = None) -> bytes:
    payload = "\n".join(json.dumps(record) for record in (records or _source_records())).encode("utf-8") + b"\n"
    path.write_bytes(payload)
    return payload


def _admission(payload: bytes, **overrides: int | str) -> LocatorAdmission:
    values: dict[str, int | str] = {
        "expected_raw_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "expected_byte_count": len(payload),
        "max_bytes": 1024 * 1024,
        "max_line_bytes": 16 * 1024,
        "max_record_count": 64,
        "max_pending_tool_calls": 8,
    }
    values.update(overrides)
    return LocatorAdmission(**values)


def _manifest(path: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "provider": "codex",
        "runtime_handle": str(path),
        "locator_hash": LOCATOR_HASH,
    }
    values.update(overrides)
    return values


def test_streaming_admission_matches_existing_codex_parse_and_evidence_semantics(tmp_path):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source)
    snapshot = admit_codex_locator_snapshot(_manifest(source), _admission(payload), project=PROJECT)

    expected_transcript = codex._parse_codex_native_jsonl(
        source, project=PROJECT, source_locator_hash=LOCATOR_HASH
    )
    assert snapshot.parsed_transcript.session == expected_transcript.session
    assert tuple(snapshot.parsed_transcript.turns) == tuple(expected_transcript.turns)
    assert tuple(snapshot.parsed_transcript.tool_events) == tuple(expected_transcript.tool_events)
    assert tuple(snapshot.parsed_transcript.parser_warnings) == tuple(expected_transcript.parser_warnings)
    assert snapshot.parsed_transcript.source_status == expected_transcript.source_status
    assert snapshot.tool_evidence == tuple(
        codex.extract_codex_tool_evidence(source, project=PROJECT, source_locator_hash=LOCATOR_HASH)
    )
    assert snapshot.raw_sha256 == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert snapshot.byte_count == len(payload)
    assert not hasattr(snapshot, "runtime_handle")


def test_streaming_admission_returns_an_immutable_activation_snapshot(tmp_path):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source)
    snapshot = admit_codex_locator_snapshot(
        _manifest(source), _admission(payload), project=PROJECT
    )

    assert isinstance(snapshot.parsed_transcript.turns, tuple)
    assert isinstance(snapshot.parsed_transcript.tool_events, tuple)
    assert isinstance(snapshot.parsed_transcript.parser_warnings, tuple)


@pytest.mark.parametrize(
    "eof_record",
    [
        _response(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "unfinished-call",
                "arguments": json.dumps({"cmd": "uv run pytest -q"}),
            }
        ),
        _response(
            {
                "type": "function_call_output",
                "call_id": "orphan-output",
                "output": "completed without an observed call",
            }
        ),
        _response(
            {
                "type": "patch_apply_end",
                "call_id": "orphan-patch-result",
                "success": True,
            }
        ),
    ],
    ids=("unfinished-call", "orphan-output", "orphan-patch-result"),
)
def test_streaming_admission_rejects_unpaired_tool_records_at_eof(tmp_path, eof_record):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source, _source_records() + [eof_record])

    with pytest.raises(ValueError, match="source_admission_failed"):
        admit_codex_locator_snapshot(_manifest(source), _admission(payload), project=PROJECT)


def test_locator_manifest_requires_exactly_three_codex_fields(tmp_path):
    source = tmp_path / "source.jsonl"
    _write_source(source)

    admitted = validate_admitted_codex_locator_manifest(_manifest(source))
    assert admitted.runtime_handle == source
    assert admitted.locator_hash == LOCATOR_HASH

    with pytest.raises(ValueError, match="source_admission_failed"):
        validate_admitted_codex_locator_manifest(_manifest(source, extra="rejected"))
    with pytest.raises(ValueError, match="source_admission_failed"):
        validate_admitted_codex_locator_manifest({"provider": "codex", "runtime_handle": str(source)})


@pytest.mark.parametrize(
    ("records", "admission_overrides"),
    [
        (_source_records(), {"max_line_bytes": 8}),
        (_source_records(), {"max_record_count": 1}),
        (_source_records(extra_calls=2), {"max_pending_tool_calls": 1}),
    ],
    ids=("line-cap", "record-cap", "pending-tool-call-cap"),
)
def test_streaming_admission_rejects_line_record_and_pending_caps(tmp_path, records, admission_overrides):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source, records)

    with pytest.raises(ValueError, match="source_admission_failed"):
        admit_codex_locator_snapshot(_manifest(source), _admission(payload, **admission_overrides), project=PROJECT)


@pytest.mark.parametrize(
    "admission_overrides",
    [
        {"expected_byte_count": 1, "max_bytes": 1},
        {"expected_raw_sha256": "sha256:" + "b" * 64},
        {"max_bytes": 256 * 1024 * 1024 + 1},
    ],
    ids=("candidate-over-cap", "expected-pre-fingerprint-mismatch", "maximum-over-256mib"),
)
def test_streaming_admission_rejects_invalid_candidate_bounds(tmp_path, admission_overrides):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source)

    with pytest.raises(ValueError, match="source_admission_failed"):
        admit_codex_locator_snapshot(
            _manifest(source),
            _admission(payload, **admission_overrides),
            project=PROJECT,
        )


def test_streaming_admission_rejects_pre_parse_evidence_post_fingerprint_drift(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    payload = _write_source(source)
    original = codex._parse_codex_turns_from_fd

    def mutate_after_parse(fd, admission, *, project, source_locator_hash):
        parsed = original(fd, admission, project=project, source_locator_hash=source_locator_hash)
        before = source.stat()
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        return parsed

    monkeypatch.setattr(codex, "_parse_codex_turns_from_fd", mutate_after_parse)

    with pytest.raises(ValueError, match="source_admission_failed"):
        admit_codex_locator_snapshot(_manifest(source), _admission(payload), project=PROJECT)
