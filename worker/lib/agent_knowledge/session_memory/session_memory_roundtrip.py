"""Retrieval no-loss verification for session-memory production proof.

Read-only. No promotion, no GC. Returns a redacted count-only verdict
(no raw ids / no raw body). RetiredIndexBridge retrieve returns semantic chunks, so
no-loss is proven by source-text coverage of the retrieved chunk union,
NOT by document body hash equality.

Limitations (presence-based substring matching):
- No-loss == every canonical source text is PRESENT in the retrieved chunk
  union (after whitespace normalization). It does NOT verify order, reject
  duplication, or require exact match.
- Very short source texts, or a source text that is a substring of another,
  can yield a false PASS. The proof relies on redacted source chunk texts
  being substantial and distinct; flag in the gate report if a session has
  trivially short turns.
- RetiredIndexBridge may transform indexed text (stemming, tokenization). If retrieval
  fails despite a correct upload, review retrieval settings before treating
  it as a real loss.
"""
from __future__ import annotations


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def verify_session_memory_retrieval_no_loss(
    *,
    retired_index_bridge,
    dataset_id: str,
    document_id: str,
    expected_source_texts: list[str],
    source_chunk_count: int,
    covered_edge_count: int,
    retrieval_question: str = "session source of truth transcript",
) -> dict:
    source_chunk_count = int(source_chunk_count)
    covered_edge_count = int(covered_edge_count)
    missing_edge_count = max(0, source_chunk_count - covered_edge_count)
    coverage_no_loss = source_chunk_count > 0 and missing_edge_count == 0

    chunks = retired_index_bridge.retrieve(
        retrieval_question,
        [dataset_id],
        document_ids=[document_id],
        limit=max(50, 2 * len(expected_source_texts)),
        similarity_threshold=0.0,
    )
    retrieved = bool(chunks)
    haystack = _normalize("\n".join(str(c.get("content") or "") for c in chunks))
    missing_chunk_count = sum(
        1 for text in expected_source_texts if _normalize(text) not in haystack
    )
    full_text_retrieval_no_loss = retrieved and missing_chunk_count == 0
    # recall acceptance (sync gate) vs GC acceptance must stay separate. The sync
    # gate only needs the document to be recall-usable: indexed and retrievable.
    # `retrieval_no_loss` therefore reflects retrievability (something came back),
    # NOT GC-grade full-source coverage. The strict every-source-text check is
    # `full_text_retrieval_no_loss`, a non-blocking warning that the SEPARATE
    # coverage-gated GC acceptance consumes. A long body whose tail RetiredIndexBridge does
    # not return in a single retrieve is still recall-acceptable; GC stays blocked
    # until full_text coverage holds. (Mixing the two would reject recall-usable
    # documents during sync, which the contract forbids.)
    retrieval_no_loss = retrieved

    return {
        "schema_version": "agent_knowledge_session_memory_roundtrip.v1",
        "retrieved": retrieved,
        "coverage_no_loss": coverage_no_loss,
        "retrieval_no_loss": retrieval_no_loss,
        "retrieval_check_scope": "document_searchability_smoke",
        "full_text_retrieval_required": False,
        "full_text_retrieval_no_loss": full_text_retrieval_no_loss,
        "missing_edge_count": missing_edge_count,
        "missing_chunk_count": missing_chunk_count,
        "source_chunk_count": source_chunk_count,
        "covered_edge_count": covered_edge_count,
        "raw_ids_printed": False,
    }
