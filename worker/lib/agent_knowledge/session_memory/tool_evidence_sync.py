from __future__ import annotations

import hashlib
from dataclasses import replace

from ..ledger import Ledger
from ..redaction import redact_text_v2
from .transcript_model import TranscriptSession, canonicalize_project
from .transcript_packer import pack_tool_evidence_summary_documents
from .transcript_parsers import extract_tool_evidence

DEFAULT_TRANSCRIPT_TARGET_PROFILE = "ragflow-transcript-memory"
TOOL_EVIDENCE_SYNC_SCHEMA_VERSION = "agent_knowledge_tool_evidence_sync.v1"


def _sha256_content(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _idempotency_key(provider: str, kind: str, content_hash: str) -> str:
    return f"{provider}:{kind}:{content_hash}"


def _conservative_metadata_value(value):
    if isinstance(value, str):
        return redact_text_v2(value)
    if isinstance(value, dict):
        return {str(key): _conservative_metadata_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_conservative_metadata_value(item) for item in value]
    return value


def _conservative_ingress_packed_document(packed):
    """Apply producer-side conservative redaction before queue enqueue."""
    return replace(
        packed,
        title=redact_text_v2(str(packed.title)),
        body=redact_text_v2(str(packed.body)),
        metadata={str(key): _conservative_metadata_value(value) for key, value in packed.metadata.items()},
        filename=redact_text_v2(str(packed.filename)),
    )


class ToolEvidenceSyncRunner:
    """Enqueue redacted tool_evidence_summary documents to an injected ingress sink.

    This is the server-owned core split out of the historical mixed
    ``transcript_ingest`` module. It does not import the monolith CLI and it does
    not perform direct RAGFlow writes; live delivery remains queue-mediated and
    caller/approval gated.
    """

    def __init__(
        self,
        *,
        ledger: Ledger,
        enqueue_sink=None,
        target_profile: str = DEFAULT_TRANSCRIPT_TARGET_PROFILE,
    ):
        self.ledger = ledger
        self.enqueue_sink = enqueue_sink
        self.target_profile = target_profile

    def run(self, *, provider: str, source_path, project: str, source_locator_hash: str) -> dict:
        project = canonicalize_project(project)
        records = extract_tool_evidence(provider, source_path, project=project, source_locator_hash=source_locator_hash)
        documents_planned = 0
        enqueued = 0
        skipped_already_indexed = 0
        job_count = 0
        knowledge_id_count = 0
        statuses: list[str] = []
        if records:
            session = TranscriptSession(
                session_id_hash=records[0].session_id_hash,
                provider=provider,
                project=project,
                started_at="",
                ended_at="",
                source_status="source_locator_private_spool_only",
                source_locator_hash=source_locator_hash,
            )
            documents = pack_tool_evidence_summary_documents(session=session, records=records)
            documents_planned = len(documents)
            for document in documents:
                content_hash = _sha256_content(document.body)
                knowledge_id = str(document.metadata.get("chunk_id") or content_hash)
                existing = self.ledger.get_by_knowledge_id(knowledge_id) or self.ledger.get_by_content_hash(content_hash)
                if (
                    self.enqueue_sink is not None
                    and existing is not None
                    and (
                        existing.get("status", "prepared") != "prepared"
                        or existing.get("ingress_job_id")
                        or existing.get("queued_at")
                    )
                ):
                    skipped_already_indexed += 1
                    statuses.append(existing.get("status", "queued"))
                    continue
                ledger_metadata = {
                    **{str(key): value for key, value in document.metadata.items()},
                    "knowledge_id": knowledge_id,
                    "chunk_id": knowledge_id,
                    "type": "tool_evidence_summary",
                    "provider": provider,
                    "project": project,
                    "session_id_hash": session.session_id_hash,
                }
                item = self.ledger.upsert_prepared(
                    knowledge_id=knowledge_id,
                    content_hash=content_hash,
                    provider=provider,
                    project=project,
                    domain="agent_memory",
                    type="tool_evidence_summary",
                    title=document.title,
                    summary=str(document.metadata.get("categories", "")),
                    privacy_level="private",
                    session_id_hash=session.session_id_hash,
                    metadata=ledger_metadata,
                )
                knowledge_id = item["knowledge_id"]
                if self.enqueue_sink is None:
                    continue
                queue_packed = replace(
                    document,
                    metadata={**document.metadata, "knowledge_id": knowledge_id, "chunk_id": knowledge_id},
                )
                queue_packed = _conservative_ingress_packed_document(queue_packed)
                queue_hash = _sha256_content(queue_packed.body)
                enqueue = self.enqueue_sink.enqueue_document(
                    source={
                        "host": "mac_mini",
                        "producer": "session-compactor",
                        "provider": provider,
                        "project": project,
                    },
                    packed=queue_packed,
                    content_hash=queue_hash,
                    target_profile=self.target_profile,
                    kind=queue_packed.kind,
                    idempotency_key=_idempotency_key(provider, queue_packed.kind, queue_hash),
                )
                job_id = str(enqueue.get("job_id", "")) if isinstance(enqueue, dict) else ""
                self.ledger.mark_enqueued(knowledge_id, target_profile=self.target_profile, job_id=job_id, run="QUEUED")
                enqueued += 1
                knowledge_id_count += 1
                statuses.append("queued")
                if job_id:
                    job_count += 1
        return {
            "schema_version": TOOL_EVIDENCE_SYNC_SCHEMA_VERSION,
            "provider": provider,
            "project": project,
            "documentKind": "tool_evidence_summary",
            "target_profile": self.target_profile,
            "documents_planned": documents_planned,
            "enqueued": enqueued,
            "skipped_already_indexed": skipped_already_indexed,
            "job_count": job_count,
            "knowledge_id_count": knowledge_id_count,
            "statuses": statuses,
            "network_used": self.enqueue_sink is not None,
            "mutation_performed": enqueued > 0,
            "ragflow_write_performed": False,
            "raw_ragflow_ids_printed": False,
        }
