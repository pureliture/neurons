"""Trial-only constant-memory bulk input pipeline. Synthetic/local use only."""

from __future__ import annotations

import inspect
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from agent_knowledge.couchdb_source.spool_reader import (
    SpoolBudgetExceeded, SpoolBusy, SpoolDiskFull, SpoolError, SpooledText,
)
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBResponseTooLarge
from agent_knowledge.llm_brain_core.bulk_full_input import (
    PrivacyGateRejected,
    SourceInvalid,
    _utf8_start,
    privacy_gate,
)
from agent_knowledge.llm_brain_core.runtime import (
    _CHUNK_METADATA_HEADER_KEYS, _conversation_chunk_order_key,
    _strip_chunk_metadata_header,
)
from agent_knowledge.llm_brain_core.bulk_spool_privacy import measure_canonical_spool
from agent_knowledge.llm_brain_core.bulk_semantic import BulkRequestLimitExceeded

SAFE_REASONS = frozenset({
    "source_invalid", "privacy_violation", "currentness_mismatch",
    "write_failure", "lease_conflict", "unknown_failure", "defer",
})


def safe_reason(exc: BaseException) -> str:
    """Map any exception to a non-sensitive allowlisted reason code."""
    if isinstance(exc, SpoolDiskFull):
        return "write_failure"
    if isinstance(exc, (SpoolBudgetExceeded, SpoolBusy, BulkRequestLimitExceeded)):
        return "defer"
    if isinstance(exc, SpoolError):
        return "source_invalid"
    if isinstance(exc, PrivacyGateRejected):
        return "privacy_violation"
    if isinstance(exc, (SourceInvalid, ValueError, KeyError)):
        return "source_invalid"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "write_failure"
    return "unknown_failure"


@dataclass(frozen=True)
class StreamedChunk:
    key: str
    ordinal: int
    lane: str
    byte_start: int
    byte_end: int
    canonical_hash: str
    text: str
    redaction_version: str
    source_hash: str = ""


def _key(prefix: str, material: str) -> str:
    return "sha256:" + hashlib.sha256(f"{prefix}|{material}".encode("utf-8")).hexdigest()


def iter_streamed_input(
    source_store: Any,
    *,
    session_id_hash: str,
    session_project: str,
    policy_version: str,
    source_hash: str,
    forbidden_markers: tuple[bytes, ...] = (),
    include_text: bool = True,
) -> Iterator[StreamedChunk]:
    """Yield one chunk at a time; never materializes the session body."""
    ordinal = 0
    cursor = 0
    for lane, doc_type in (
        ("conversation", "conversation_chunk"),
        ("evidence", "tool_evidence_bundle"),
    ):
        for doc in source_store.iter_by_session(
            session_id_hash=session_id_hash, doc_type=doc_type, page_size=64
        ):
            raw = str(doc.get("body") or "")
            redaction_version = str(doc.get("redaction_version") or "")
            if not redaction_version:
                raise SourceInvalid("missing redaction provenance")
            if lane == "conversation":
                raw = _strip_chunk_metadata_header(raw)
            data = raw.encode("utf-8")
            privacy_gate(data, forbidden_markers=forbidden_markers, all_redacted=True)
            if lane == "conversation" and ordinal:
                cursor += 1  # separator byte belongs to the following range
            start = cursor
            cursor += len(data)
            canonical_hash = "sha256:" + hashlib.sha256(data).hexdigest()
            key = _key(
                f"{session_id_hash}:{lane}:{ordinal}",
                f"{policy_version}|{source_hash}|{canonical_hash}",
            )
            yield StreamedChunk(
                key=key,
                ordinal=ordinal,
                lane=lane,
                text=data.decode("utf-8") if include_text else "",
                byte_end=cursor,
                byte_start=start,
                canonical_hash=canonical_hash,
                redaction_version=redaction_version,
            )
            ordinal += 1


def iter_batches(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

GET_MANY_RESPONSE_BYTE_CAP = 1024 * 1024

GET_MANY_BODY_BUDGET = 512 * 1024


class _BatchTooLarge(Exception):
    pass


def _fetch_with_split(get_many, members: list[Any], source_store=None) -> dict[str, Any]:
    doc_ids = [member.doc_id for member in members]
    try:
        if "max_response_bytes" in inspect.signature(get_many).parameters:
            fetched = get_many(
                doc_ids, page_size=max(1, len(doc_ids)),
                max_response_bytes=GET_MANY_RESPONSE_BYTE_CAP,
            )
        else:
            fetched = get_many(doc_ids, page_size=max(1, len(doc_ids)))
        payload_bytes = sum(
            len(str((fetched.get(doc_id) or {}).get("body") or "").encode("utf-8"))
            for doc_id in doc_ids
        )
        if payload_bytes > GET_MANY_BODY_BUDGET:
            raise _BatchTooLarge()
        return fetched
    except (CouchDBResponseTooLarge, _BatchTooLarge):
        if len(members) == 1 and source_store is not None and hasattr(source_store, "open_document_spool"):
            member = members[0]
            body = source_store.open_document_spool(
                member.doc_id, expected_hash=member.content_hash
            )
            return {member.doc_id: {
                "body": SpooledText(body), "content_hash": body.digest,
                "redaction_version": body.metadata.get("redaction_version", ""),
            }}
        if len(members) == 1:
            raise SourceInvalid(
                f"single source document exceeds response cap "
                f"({GET_MANY_RESPONSE_BYTE_CAP} bytes) or body budget "
                f"({GET_MANY_BODY_BUDGET} bytes)"
            ) from None
        middle = len(members) // 2
        merged = dict(_fetch_with_split(get_many, members[:middle], source_store))
        merged.update(_fetch_with_split(get_many, members[middle:], source_store))
        return merged

def _validate_spooled_body(body: SpooledText, member: Any) -> None:
    if body.content_hash != member.content_hash:
        body.close()
        raise SourceInvalid("member body hash mismatch")
    raw_hasher = hashlib.sha256()
    raw_total = 0
    try:
        for page in body.iter_pages():
            page_data = page.encode("utf-8")
            raw_hasher.update(page_data)
            raw_total += len(page_data)
    except Exception:
        body.close()
        raise
    if ("sha256:" + raw_hasher.hexdigest() != member.content_hash
            or raw_total != body.byte_length):
        body.close()
        raise SourceInvalid("member body coverage mismatch")


def _iter_utf8_byte_slices(pages: Iterable[str], max_chunk_bytes: int) -> Iterator[bytes]:
    """Yield bounded UTF-8 byte slices without cutting inside a codepoint."""
    if max_chunk_bytes < 4:
        raise ValueError("max_chunk_bytes must be >= 4")
    buf = bytearray()
    yielded = False
    for page in pages:
        if not page:
            continue
        buf.extend(page.encode("utf-8"))
        while len(buf) > max_chunk_bytes:
            cut = max_chunk_bytes
            while cut > 0 and (buf[cut] & 0xC0) == 0x80:
                cut -= 1
            if cut == 0:
                raise ValueError("invalid UTF-8 boundary while splitting chunk")
            yield bytes(buf[:cut])
            del buf[:cut]
            yielded = True
    if buf or not yielded:
        yield bytes(buf)


def iter_planned_chunks(
    source_store: Any,
    plan: Any,
    *,
    forbidden_markers: tuple[bytes, ...] = (),
    max_chunk_bytes: int | None = None,
) -> Iterator[StreamedChunk]:
    """Yield validated bodies one at a time using bounded batch reads."""
    if max_chunk_bytes is not None and max_chunk_bytes < 4:
        raise ValueError("max_chunk_bytes must be >= 4")
    cursor = 0
    get_many = getattr(source_store, "get_many", None)
    members = list(plan.members)
    index = 0
    while index < len(members):
        batch = members[index:index + 8]
        if get_many is not None:
            fetched = _fetch_with_split(get_many, batch, source_store)
        else:
            fetched = {member.doc_id: source_store.get(member.doc_id) for member in batch}
        for member in batch:
            doc = fetched.get(member.doc_id)
            if not doc:
                raise SourceInvalid("planned member disappeared")
            body = doc.get("body")
            if isinstance(body, SpooledText):
                _validate_spooled_body(body, member)
                canonical, canonical_digest, total = measure_canonical_spool(
                    body, lane=member.lane, forbidden_markers=forbidden_markers,
                )
                if max_chunk_bytes is not None and total > max_chunk_bytes:
                    try:
                        if member.lane == "conversation" and member.ordinal:
                            cursor += 1
                        for part_index, piece in enumerate(
                            _iter_utf8_byte_slices(canonical.iter_pages(), max_chunk_bytes)
                        ):
                            start = cursor
                            cursor += len(piece)
                            piece_digest = "sha256:" + hashlib.sha256(piece).hexdigest()
                            yield StreamedChunk(
                                key=planned_chunk_key(plan, member, part_index=part_index),
                                ordinal=member.ordinal,
                                lane=member.lane,
                                byte_start=start,
                                byte_end=cursor,
                                canonical_hash=piece_digest,
                                source_hash=member.content_hash,
                                text=piece.decode("utf-8"),
                                redaction_version=member.redaction_version,
                            )
                    finally:
                        canonical.close()
                    continue
                if member.lane == "conversation" and member.ordinal:
                    cursor += 1
                start = cursor
                cursor += total
                key = planned_chunk_key(plan, member)
                yield StreamedChunk(
                    key=key, ordinal=member.ordinal, lane=member.lane,
                    byte_start=start, byte_end=cursor,
                    canonical_hash=canonical_digest, source_hash=member.content_hash,
                    text=canonical, redaction_version=member.redaction_version,
                )
                continue
            raw = str(body or "")
            source_digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if source_digest != member.content_hash:
                raise SourceInvalid("member body hash mismatch")
            if member.lane == "conversation":
                raw = _strip_chunk_metadata_header(raw)
            data = raw.encode("utf-8")
            privacy_gate(data, forbidden_markers=forbidden_markers, all_redacted=True)
            if max_chunk_bytes is not None and len(data) > max_chunk_bytes:
                if member.lane == "conversation" and member.ordinal:
                    cursor += 1
                for part_index, piece in enumerate(
                    _iter_utf8_byte_slices([data.decode("utf-8")], max_chunk_bytes)
                ):
                    start = cursor
                    cursor += len(piece)
                    piece_digest = "sha256:" + hashlib.sha256(piece).hexdigest()
                    yield StreamedChunk(
                        key=planned_chunk_key(plan, member, part_index=part_index),
                        ordinal=member.ordinal,
                        lane=member.lane,
                        byte_start=start,
                        byte_end=cursor,
                        canonical_hash=piece_digest,
                        source_hash=member.content_hash,
                        text=piece.decode("utf-8"),
                        redaction_version=member.redaction_version,
                    )
                continue
            canonical_digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if member.lane == "conversation" and member.ordinal:
                cursor += 1
            start = cursor
            cursor += len(data)
            key = planned_chunk_key(plan, member)
            yield StreamedChunk(
                key=key, ordinal=member.ordinal, lane=member.lane,
                byte_start=start, byte_end=cursor,
                canonical_hash=canonical_digest, source_hash=member.content_hash,
                text=data.decode("utf-8"), redaction_version=member.redaction_version,
            )
        index += len(batch)


def planned_chunk_key(plan: Any, member: Any, *, part_index: int | None = None) -> str:
    suffix = f":p{part_index}" if part_index is not None else ""
    return _key(
        f"{plan.source_hash}:{member.lane}:{member.ordinal}:{member.doc_id}{suffix}",
        member.content_hash,
    )


def expected_source_bytes(
    source_store: Any,
    plan: Any,
    *,
    forbidden_markers: tuple[bytes, ...] = (),
    max_chunk_bytes: int | None = None,
) -> dict[str, Any]:
    """Measure the pinned plan's canonical bytes without building model input.

    Bodies are fetched in bounded batches and released per member; only hashes,
    lengths, and the planned order are retained. Conversation separators are
    excluded from ``body_bytes`` and reported separately so callers can state
    whether the full streamed byte count includes them.
    """
    if max_chunk_bytes is not None and max_chunk_bytes < 4:
        raise ValueError("max_chunk_bytes must be >= 4")
    ordered: list[dict[str, Any]] = []
    body_bytes = 0
    separator_bytes = 0
    get_many = getattr(source_store, "get_many", None)
    members = list(plan.members)
    index = 0
    while index < len(members):
        batch = members[index:index + 8]
        if get_many is not None:
            fetched = _fetch_with_split(get_many, batch, source_store)
        else:
            fetched = {member.doc_id: source_store.get(member.doc_id) for member in batch}
        for member in batch:
            doc = fetched.get(member.doc_id)
            if not doc:
                raise SourceInvalid("planned member disappeared")
            body = doc.get("body")
            if member.lane == "conversation" and member.ordinal:
                separator_bytes += 1
            if isinstance(body, SpooledText):
                _validate_spooled_body(body, member)
                canonical, canonical_hash, size = measure_canonical_spool(
                    body, lane=member.lane, forbidden_markers=forbidden_markers,
                )
                try:
                    if max_chunk_bytes is not None and size > max_chunk_bytes:
                        for part_index, piece in enumerate(
                            _iter_utf8_byte_slices(canonical.iter_pages(), max_chunk_bytes)
                        ):
                            part_len = len(piece)
                            body_bytes += part_len
                            ordered.append({
                                "key": planned_chunk_key(plan, member, part_index=part_index),
                                "canonical_hash": "sha256:" + hashlib.sha256(piece).hexdigest(),
                                "byte_length": part_len,
                                "lane": member.lane,
                            })
                        continue
                finally:
                    canonical.close()
            else:
                raw = str(body or "")
                if ("sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
                        != member.content_hash):
                    raise SourceInvalid("member body hash mismatch")
                if member.lane == "conversation":
                    raw = _strip_chunk_metadata_header(raw)
                data = raw.encode("utf-8")
                privacy_gate(data, forbidden_markers=forbidden_markers, all_redacted=True)
                if max_chunk_bytes is not None and len(data) > max_chunk_bytes:
                    for part_index, piece in enumerate(
                        _iter_utf8_byte_slices([data.decode("utf-8")], max_chunk_bytes)
                    ):
                        part_len = len(piece)
                        body_bytes += part_len
                        ordered.append({
                            "key": planned_chunk_key(plan, member, part_index=part_index),
                            "canonical_hash": "sha256:" + hashlib.sha256(piece).hexdigest(),
                            "byte_length": part_len,
                            "lane": member.lane,
                        })
                    continue
                canonical_hash = "sha256:" + hashlib.sha256(data).hexdigest()
                size = len(data)
            body_bytes += size
            ordered.append({
                "key": planned_chunk_key(plan, member),
                "canonical_hash": canonical_hash,
                "byte_length": size,
                "lane": member.lane,
            })
        index += len(batch)
    return {
        "ordered": ordered,
        "body_bytes": body_bytes,
        "separator_bytes": separator_bytes,
        "full_bytes": body_bytes + separator_bytes,
        "zero_byte_members": sum(1 for item in ordered if item["byte_length"] == 0),
    }
