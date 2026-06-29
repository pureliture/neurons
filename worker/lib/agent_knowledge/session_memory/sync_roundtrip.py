from __future__ import annotations

from .memory_regeneration import (
    _canonical_session_group_for_memory,
    _canonicalize_session_chunks_for_memory,
    _group_by_session,
    _knowledge_id_for_session_memory,
    _sanitize_session_memory_chunk_text,
    _sectioned_tool_evidence_lines,
    _sha256_content,
    pack_session_memory_document,
)
from .session_memory_roundtrip import verify_session_memory_retrieval_no_loss


def rollback_session_memory_document(ledger, retired_index_bridge, dataset_id: str, session_id_hash: str) -> dict:
    getter = getattr(ledger, "get_session_memory_by_session_id_hash", None)
    row = getter(session_id_hash) if callable(getter) else None
    if not row:
        return {"rolled_back": False, "disable_status": "no_document_id"}
    return rollback_session_memory_document_by_knowledge_id(
        ledger,
        retired_index_bridge,
        dataset_id,
        str(row.get("knowledge_id") or ""),
    )


def rollback_session_memory_document_by_knowledge_id(ledger, retired_index_bridge, dataset_id: str, knowledge_id: str) -> dict:
    if not knowledge_id:
        return {"rolled_back": False, "disable_status": "no_document_id"}
    row = ledger.get_by_knowledge_id(knowledge_id)
    if not row:
        return {"rolled_back": False, "disable_status": "no_document_id"}
    document_id = str(row.get("index_document_id") or "")
    if not document_id:
        ledger.mark_disabled(knowledge_id)
        return {"rolled_back": False, "disable_status": "ledger_disabled_no_document"}
    try:
        retired_index_bridge.disable_document(dataset_id, document_id)
        ledger.mark_disabled(knowledge_id)
        return {"rolled_back": True, "disable_status": "disabled"}
    except Exception:
        return {"rolled_back": False, "disable_status": "disable_failed"}


def verify_session_memory_sync_roundtrip(
    *,
    ledger,
    retired_index_bridge,
    dataset_id: str,
    session_id_hash: str,
    source,
    provider: str | None = None,
    project: str | None = None,
) -> dict:
    source_chunks = source.list_conversation_chunks(
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
    )
    groups = _group_by_session(source_chunks)
    if not groups:
        return {
            "schema_version": "agent_knowledge_session_memory_roundtrip.v1",
            "retrieved": False,
            "coverage_no_loss": False,
            "retrieval_no_loss": False,
            "missing_chunk_count": -1,
            "reason": "source_session_unresolved",
            "rolled_back": False,
            "disable_status": "no_document_id",
        }
    canonical_group, _ = _canonical_session_group_for_memory(groups[0])
    evidence_lister = getattr(source, "list_tool_evidence_summaries", None)
    evidence = []
    if callable(evidence_lister):
        evidence = evidence_lister(
            project=canonical_group.project,
            provider=canonical_group.provider,
            session_id_hash=canonical_group.session_id_hash,
        )
    knowledge_id = _knowledge_id_for_session_memory(canonical_group, evidence=evidence)
    row = ledger.get_by_knowledge_id(knowledge_id)
    if row is None:
        packed = pack_session_memory_document(canonical_group, evidence=evidence)
        row = ledger.get_by_content_hash(_sha256_content(packed.body))
    if not row:
        return {
            "schema_version": "agent_knowledge_session_memory_roundtrip.v1",
            "retrieved": False,
            "coverage_no_loss": False,
            "retrieval_no_loss": False,
            "missing_chunk_count": -1,
            "reason": "session_memory_identity_unresolved",
            "rolled_back": False,
            "disable_status": "no_document_id",
        }
    knowledge_id = str(row.get("knowledge_id") or knowledge_id)
    document_id = str(row.get("index_document_id") or "")
    if not document_id:
        rollback = rollback_session_memory_document_by_knowledge_id(ledger, retired_index_bridge, dataset_id, knowledge_id)
        return {
            "schema_version": "agent_knowledge_session_memory_roundtrip.v1",
            "retrieved": False,
            "coverage_no_loss": False,
            "retrieval_no_loss": False,
            "missing_chunk_count": -1,
            "reason": "document_not_uploaded",
            **rollback,
        }
    source_chunk_count = int(row.get("source_chunk_count") or 0)
    edges = ledger.list_session_memory_coverage(knowledge_id)
    canonical_source_chunks, _ = _canonicalize_session_chunks_for_memory(source_chunks)
    expected_source_texts = [
        _sanitize_session_memory_chunk_text(str(getattr(chunk, "redacted_text", "") or ""))
        for chunk in canonical_source_chunks
    ]
    try:
        verdict = verify_session_memory_retrieval_no_loss(
            retired_index_bridge=retired_index_bridge,
            dataset_id=dataset_id,
            document_id=document_id,
            expected_source_texts=expected_source_texts,
            source_chunk_count=source_chunk_count,
            covered_edge_count=len(edges),
        )
        verdict["schema_version"] = "agent_knowledge_session_memory_roundtrip.v1"
    except Exception:
        verdict = {
            "schema_version": "agent_knowledge_session_memory_roundtrip.v1",
            "retrieved": False,
            "coverage_no_loss": False,
            "retrieval_no_loss": False,
            "missing_chunk_count": -1,
            "reason": "retrieval_failed",
        }
    evidence_texts = _session_memory_tool_evidence_expected_texts(_sectioned_tool_evidence_lines(evidence))
    missing_tool_evidence_count = 0
    tool_evidence_retrieved = not evidence_texts
    if evidence_texts:
        try:
            evidence_chunks = retired_index_bridge.retrieve(
                "session memory tool evidence",
                [dataset_id],
                document_ids=[document_id],
                limit=max(50, 2 * len(evidence_texts)),
                similarity_threshold=0.0,
            )
            evidence_haystack = _compact_roundtrip_text("\n".join(str(chunk.get("content") or "") for chunk in evidence_chunks))
            missing_tool_evidence_count = sum(
                1 for text in evidence_texts if _compact_roundtrip_text(text) not in evidence_haystack
            )
            tool_evidence_retrieved = bool(evidence_chunks)
        except Exception:
            missing_tool_evidence_count = len(evidence_texts)
            tool_evidence_retrieved = False
    tool_evidence_no_loss = missing_tool_evidence_count == 0 and tool_evidence_retrieved
    verdict["tool_evidence_count"] = len(evidence_texts)
    verdict["missing_tool_evidence_count"] = missing_tool_evidence_count
    verdict["tool_evidence_no_loss"] = tool_evidence_no_loss
    verdict["retrieval_no_loss"] = bool(verdict.get("retrieval_no_loss")) and tool_evidence_no_loss
    if not (verdict["coverage_no_loss"] and verdict["retrieval_no_loss"]):
        verdict.update(rollback_session_memory_document_by_knowledge_id(ledger, retired_index_bridge, dataset_id, knowledge_id))
    return verdict


def _compact_roundtrip_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _session_memory_tool_evidence_expected_texts(lines: list[str]) -> list[str]:
    texts: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("### "):
            if current:
                texts.append("\n".join(current).strip())
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        texts.append("\n".join(current).strip())
    return [text for text in texts if text]
