"""관측·해석 identity와 표시 위치를 분리한 source 참조.

본문이나 credential을 복제하지 않는다. hash/offset 무결성은 진위 인증이나
현재 사실의 검증을 대신하지 않으며, native anchor가 없는 기록을 추정 병합하지 않는다.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re
from typing import Mapping


SCHEMA = "tool_observation_ref.v1"
HASH = re.compile(r"sha256:[0-9a-f]{64}")
VERSION = re.compile(r"[A-Za-z0-9_.:+-]{1,128}")
_FIELDS = frozenset({"schema_version", "session_id_hash", "provider", "project", "observation_id_hash",
    "evidence_id_hash", "source_event_id_hash", "source_content_hash", "record_content_hash",
    "extractor_version", "redaction_version", "source_event_index", "evidence_index", "observed_at",
    "span_start", "span_end", "span_content_hash"})


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def observed_time(value):
    """잘못된 시각을 현재 시각으로 보정하지 않는다."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else ""
    except ValueError:
        return ""


def from_record(record, *, body, start, end):
    reference = {"schema_version": SCHEMA, "session_id_hash": record.session_id_hash,
        "provider": record.provider, "project": record.project,
        "observation_id_hash": record.observation_id_hash, "evidence_id_hash": record.evidence_id_hash,
        "source_event_id_hash": record.source_event_id_hash, "source_content_hash": record.source_content_hash,
        "record_content_hash": record.content_hash, "extractor_version": record.extractor_version,
        "redaction_version": record.redaction_version, "source_event_index": record.source_event_index,
        "evidence_index": record.evidence_index, "observed_at": observed_time(record.observed_at),
        "span_start": start, "span_end": end, "span_content_hash": digest(body[start:end])}
    return validate([reference], body=body, session_id_hash=record.session_id_hash,
        provider=record.provider, project=record.project)[0]


def validate(references, *, body, session_id_hash, provider, project):
    if not isinstance(references, (list, tuple)) or len(references) > 10000 or not isinstance(body, str):
        raise ValueError("bounded observation references required")
    result = []
    seen = set()
    for reference in references:
        if not isinstance(reference, Mapping) or set(reference) != _FIELDS or reference["schema_version"] != SCHEMA:
            raise ValueError("observation reference schema invalid")
        if (reference["session_id_hash"], reference["provider"], reference["project"]) != (session_id_hash, provider, project):
            raise ValueError("observation reference scope mismatch")
        for name in ("session_id_hash", "evidence_id_hash", "record_content_hash", "span_content_hash"):
            if not isinstance(reference[name], str) or not HASH.fullmatch(reference[name]):
                raise ValueError("observation reference digest invalid")
        for name in ("observation_id_hash", "source_event_id_hash", "source_content_hash"):
            if not isinstance(reference[name], str) or (reference[name] and not HASH.fullmatch(reference[name])):
                raise ValueError("native observation reference digest invalid")
        if bool(reference["source_event_id_hash"]) != bool(reference["source_content_hash"]):
            raise ValueError("native observation source pair required")
        expected = (digest(f"tool_observation.v1:{session_id_hash}:{reference['source_event_id_hash']}")
            if reference["source_event_id_hash"] else "")
        if reference["observation_id_hash"] != expected:
            raise ValueError("native observation identity mismatch")
        for name in ("extractor_version", "redaction_version"):
            if not isinstance(reference[name], str) or not VERSION.fullmatch(reference[name]):
                raise ValueError("observation interpretation version invalid")
        for name in ("evidence_index", "span_start", "span_end"):
            if type(reference[name]) is not int or reference[name] < 0:
                raise ValueError("observation position invalid")
        event_index = reference["source_event_index"]
        if event_index is not None and (type(event_index) is not int or event_index < 0):
            raise ValueError("native observation position invalid")
        if not isinstance(reference["observed_at"], str) or observed_time(reference["observed_at"]) != reference["observed_at"]:
            raise ValueError("observation time invalid")
        start, end = reference["span_start"], reference["span_end"]
        if not 0 <= start <= end <= len(body) or digest(body[start:end]) != reference["span_content_hash"]:
            raise ValueError("observation span integrity mismatch")
        identity = (reference["evidence_id_hash"], start, end)
        if identity in seen:
            raise ValueError("duplicate observation reference position")
        seen.add(identity)
        result.append(copy.deepcopy(dict(reference)))
    return result


def coverage_hash(content_hashes, references):
    """본문이 같아도 native 호출/해석 version이 다르면 같은 source로 덮어쓰지 않는다."""
    return digest(json.dumps({"schema_version": SCHEMA, "record_content_hashes": list(content_hashes),
        "observation_refs": list(references)}, sort_keys=True, ensure_ascii=True, separators=(",", ":")))


def identity(reference):
    """rechunk/표시 위치를 identity로 사용하지 않는다."""
    return (reference["project"], reference["provider"], reference["session_id_hash"], reference["evidence_id_hash"])
