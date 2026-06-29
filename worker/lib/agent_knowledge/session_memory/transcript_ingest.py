from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..ledger import Ledger
from ..redaction import redact_public_ingress_text
from .tool_evidence_sync import DEFAULT_TRANSCRIPT_TARGET_PROFILE
from .transcript_chunking import build_transcript_chunks, knowledge_id_for_chunk
from .transcript_model import TranscriptChunk
from .transcript_packer import pack_conversation_chunk_document
from .transcript_parsers import ParsedTranscript, parse_transcript_source

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:token|secret|password|api[_-]?key)[A-Z0-9_]*\s*=\s*[^\s,;]+"
)


@dataclass(frozen=True)
class TranscriptIngestResult:
    request_id: str = ""
    knowledge_ids: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    status: str = ""
    error_class: str = ""
    message: str = ""
    rejected_chunk_count: int = 0
    rejected_chunk_ids: list[str] = field(default_factory=list)


class TranscriptIngestWorker:
    """Server-owned transcript build + injected enqueue core.

    The historical monolith worker also carried client HTTP enqueue wiring and
    direct RetiredIndexBridge indexing. This neurons slice deliberately keeps those out:
    callers provide a server-owned sink such as the state-DB ingress sink, and
    this worker only parses, chunks, records ledger state, and invokes that sink.
    """

    def __init__(
        self,
        *,
        capture_spool,
        ledger: Ledger,
        enqueue_sink,
        target_profile: str = DEFAULT_TRANSCRIPT_TARGET_PROFILE,
    ):
        if ledger is None:
            raise ValueError("server transcript ingest requires a ledger")
        if enqueue_sink is None:
            raise ValueError("server transcript ingest requires an enqueue sink")
        self.capture_spool = capture_spool
        self.ledger = ledger
        self.enqueue_sink = enqueue_sink
        self.target_profile = target_profile

    def run_once(self) -> TranscriptIngestResult:
        claimed = self.capture_spool.claim_next()
        try:
            request = validate_transcript_ingest_request(json.loads(claimed.read_text(encoding="utf-8")))
            source_path = _source_path_from_request(request)
            parsed = parse_transcript_source(
                request["provider"],
                source_path,
                project=request["project"],
                source_locator_hash=request["source_locator"].get("locator_hash", ""),
            )
            result = self.index_parsed_transcript(request=request, parsed=parsed)
            self.capture_spool.ack(claimed)
            return result
        except Exception as exc:
            if claimed.exists():
                _quarantine(self.capture_spool, claimed, _quarantine_failure_record(exc))
            return TranscriptIngestResult(
                request_id=_safe_request_id(claimed),
                status="quarantined",
                error_class=_classify_error(exc),
                message=_redacted_error_message(exc),
            )

    def index_parsed_transcript(self, *, request: dict, parsed: ParsedTranscript) -> TranscriptIngestResult:
        self.ledger.upsert_transcript_session(parsed.session)
        for turn in parsed.turns:
            self.ledger.upsert_transcript_turn(turn)
        for tool_event in parsed.tool_events:
            self.ledger.upsert_transcript_tool_event(tool_event)

        knowledge_ids: list[str] = []
        job_ids: list[str] = []
        statuses: list[str] = []
        rejected_chunk_ids: list[str] = []
        turn_by_index = {turn.turn_index: turn for turn in parsed.turns}
        tool_events_by_turn_id = _tool_events_by_turn_id(parsed)
        for chunk in build_transcript_chunks(parsed):
            knowledge_id = knowledge_id_for_chunk(chunk)
            item = self.ledger.upsert_transcript_chunk(knowledge_id=knowledge_id, chunk=chunk)
            existing = item or self.ledger.get_by_content_hash(chunk.content_hash)
            if (
                existing
                and existing.get("status") == "queued"
                and existing.get("ingress_job_id")
            ):
                knowledge_ids.append(existing["knowledge_id"])
                job_ids.append(existing["ingress_job_id"])
                statuses.append("queued")
                continue
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
            packed = pack_conversation_chunk_document(
                session=parsed.session,
                turns=turns,
                tool_events=tool_events,
                chunk_id=chunk.chunk_id,
                knowledge_id=knowledge_id,
                capture_request_id=request.get("request_id", ""),
                chunk_redacted_text=chunk.redacted_text,
                part_index=chunk.part_index,
                part_count=chunk.part_count,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
            queue_packed = _conservative_ingress_packed_document(packed)
            content_hash = _sha256_content(queue_packed.body)
            try:
                enqueue = self.enqueue_sink.enqueue_document(
                    source=_queue_source(parsed),
                    packed=queue_packed,
                    content_hash=content_hash,
                    target_profile=self.target_profile,
                    kind=queue_packed.kind,
                    idempotency_key=_idempotency_key(parsed.session.provider, queue_packed.kind, content_hash),
                )
            except Exception:
                rejected_chunk_ids.append(chunk.chunk_id)
                continue
            job_id = str(enqueue.get("job_id", "")) if isinstance(enqueue, dict) else ""
            self.ledger.mark_enqueued(knowledge_id, target_profile=self.target_profile, job_id=job_id, run="QUEUED")
            knowledge_ids.append(knowledge_id)
            statuses.append("queued")
            if job_id:
                job_ids.append(job_id)

        return TranscriptIngestResult(
            request_id=str(request.get("request_id") or ""),
            knowledge_ids=knowledge_ids,
            job_ids=job_ids,
            status=_combined_status(statuses),
            rejected_chunk_count=len(rejected_chunk_ids),
            rejected_chunk_ids=rejected_chunk_ids,
        )


def validate_transcript_ingest_request(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("source_parse_failed: request root must be an object")
    for key in ("request_id", "provider", "project", "source_locator"):
        if not request.get(key):
            raise ValueError(f"source_parse_failed: missing {key}")
    if not isinstance(request["source_locator"], dict):
        raise ValueError("source_parse_failed: source_locator must be an object")
    if not request["source_locator"].get("runtime_handle"):
        raise ValueError("source_unproven")
    return request


def _source_path_from_request(request: dict) -> Path:
    source_locator = request["source_locator"]
    runtime_handle = source_locator.get("runtime_handle")
    if not runtime_handle:
        raise ValueError("source_unproven")
    path = Path(runtime_handle)
    if path.is_symlink():
        raise ValueError("source_policy_blocked")
    if not path.exists() or not path.is_file():
        raise ValueError("source_unreadable")
    return path


def _conservative_ingress_packed_document(packed):
    return replace(
        packed,
        title=_server_ingress_redact(str(packed.title)),
        body=_server_ingress_redact(str(packed.body)),
        metadata={str(key): _conservative_metadata_value(value) for key, value in packed.metadata.items()},
        filename=_server_ingress_redact(str(packed.filename)),
    )


def _conservative_metadata_value(value):
    if isinstance(value, str):
        return _server_ingress_redact(value)
    if isinstance(value, dict):
        return {str(key): _conservative_metadata_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_conservative_metadata_value(item) for item in value]
    return value


def _server_ingress_redact(value: str) -> str:
    return _SECRET_ASSIGNMENT_RE.sub("<redacted-secret-assignment>", redact_public_ingress_text(value))


def _tool_events_by_turn_id(parsed: ParsedTranscript) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for event in parsed.tool_events:
        grouped.setdefault(event.turn_id_hash, []).append(event)
    for events in grouped.values():
        events.sort(key=lambda item: item.event_index)
    return grouped


def _queue_source(parsed: ParsedTranscript) -> dict[str, str]:
    return {
        "host": "neurons-worker",
        "producer": "server-transcript-ingest",
        "provider": parsed.session.provider,
        "project": parsed.session.project,
    }


def _sha256_content(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _idempotency_key(provider: str, kind: str, content_hash: str) -> str:
    return f"{provider}:{kind}:{content_hash}"


def _combined_status(statuses: list[str]) -> str:
    if not statuses:
        return "index_timeout"
    if any(status == "queued" for status in statuses):
        return "queued"
    if all(status == "indexed" for status in statuses):
        return "indexed"
    return statuses[-1]


def _classify_error(exc: Exception) -> str:
    message = str(exc)
    for marker in ("source_unreadable", "source_unproven", "source_policy_blocked", "source_parse_failed"):
        if marker in message:
            return marker
    return exc.__class__.__name__


def _redacted_error_message(exc: Exception) -> str:
    error_class = _classify_error(exc)
    if error_class.startswith("source_"):
        return error_class
    return "transcript ingest failed"


def _quarantine_failure_record(exc: Exception) -> dict:
    return {
        "error_class": _classify_error(exc),
        "message": _redacted_error_message(exc),
        "recoverable": False,
    }


def _safe_request_id(path: Path) -> str:
    return path.stem if path.name else ""


def _quarantine(spool, claimed: Path, failure: dict) -> None:
    if hasattr(spool, "quarantine_with_failure"):
        spool.quarantine_with_failure(claimed, failure)
    else:
        spool.quarantine(claimed)
