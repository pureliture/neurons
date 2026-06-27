from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ._util import ensure_public_safe, public_safe_text, short_hash

DocumentStatus = Literal[
    "source_of_truth",
    "active",
    "generated_companion",
    "human_preview",
    "historical",
    "superseded",
    "stale",
    "archive_candidate",
    "unknown",
]

DocumentEvidenceType = Literal[
    "memory_card",
    "session",
    "commit",
    "pull_request",
    "live",
    "file_inventory",
    "source_ref",
    "unknown",
]


@dataclass(frozen=True)
class DocumentEvidenceEdge:
    document_path: str
    evidence_type: DocumentEvidenceType
    evidence_ref: str
    relation: str = "supports_status"
    confidence: float = 0.0

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "DocumentEvidenceEdge")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentAuthorityCard:
    path: str
    status: DocumentStatus
    reason: str
    confidence: float
    evidence_refs: tuple[str, ...]
    evidence_edges: tuple[DocumentEvidenceEdge, ...] = ()
    archive_proposal_only: bool = True

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "DocumentAuthorityCard")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["evidence_edges"] = [edge.to_dict() if isinstance(edge, DocumentEvidenceEdge) else edge for edge in self.evidence_edges]
        return data


def document_authority_cards_from_memory_cards(
    cards: list[dict[str, Any]],
    *,
    inventory_paths: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    documents: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        ref = safe_authority_ref(payload.get("authority_ref"))
        if not ref or ref in seen:
            continue
        status, reason = classify_document_card(card, ref)
        edges = document_evidence_edges_from_memory_card(card, document_path=ref)
        documents.append(
            DocumentAuthorityCard(
                path=ref,
                status=status,
                reason=reason,
                confidence=float(card.get("confidence") or 0),
                evidence_refs=tuple(edge.evidence_ref for edge in edges),
                evidence_edges=tuple(edges),
            ).to_dict()
        )
        seen.add(ref)
    for path in inventory_paths:
        ref = safe_authority_ref(path)
        if not ref or ref in seen or not _is_document_path(ref):
            continue
        status, reason = classify_document_path(ref)
        evidence_ref = f"file_inventory:{ref}"
        edge = DocumentEvidenceEdge(
            document_path=ref,
            evidence_type="file_inventory",
            evidence_ref=evidence_ref,
            confidence=0.5,
        )
        documents.append(
            DocumentAuthorityCard(
                path=ref,
                status=status,
                reason=reason,
                confidence=0.5,
                evidence_refs=(evidence_ref,),
                evidence_edges=(edge,),
            ).to_dict()
        )
        seen.add(ref)
    return documents


def classify_document_path(path: str) -> tuple[DocumentStatus, str]:
    text = str(path or "").lower()
    name = text.rsplit("/", 1)[-1]
    if name in {"requirements.md", "design.md", "roadmap.md"}:
        return "source_of_truth", "approved_markdown_source"
    if text.endswith(".html"):
        return "generated_companion", "html_preview_or_generated_companion"
    if text.endswith(".md"):
        return "active", "markdown_document"
    return "unknown", "document_status_unknown"


def classify_document_card(card: Mapping[str, Any], path: str) -> tuple[DocumentStatus, str]:
    currentness = str(card.get("currentness") or "").lower()
    if currentness in {"stale", "superseded", "archive_candidate"}:
        return "archive_candidate", "stale_or_superseded_memory_card"
    return classify_document_path(path)


def _is_document_path(path: str) -> bool:
    text = str(path or "").lower()
    return text.endswith((".md", ".html"))


def document_evidence_edges_from_memory_card(
    card: Mapping[str, Any],
    *,
    document_path: str,
) -> list[DocumentEvidenceEdge]:
    confidence = float(card.get("confidence") or 0)
    edges = [
        DocumentEvidenceEdge(
            document_path=document_path,
            evidence_type="memory_card",
            evidence_ref=str(card.get("memory_id") or ""),
            confidence=confidence,
        )
    ]
    for ref in card.get("source_refs") or []:
        edge = _edge_from_ref(ref, document_path=document_path, confidence=confidence, default_type="source_ref")
        if edge is not None:
            edges.append(edge)
    for ref in card.get("evidence_refs") or []:
        edge = _edge_from_ref(ref, document_path=document_path, confidence=confidence, default_type="unknown")
        if edge is not None:
            edges.append(edge)
    return _dedupe_edges(edges)


def _edge_from_ref(
    ref: Any,
    *,
    document_path: str,
    confidence: float,
    default_type: DocumentEvidenceType,
) -> DocumentEvidenceEdge | None:
    if isinstance(ref, Mapping):
        evidence_ref = (
            ref.get("source_ref_id")
            or ref.get("id")
            or ref.get("knowledge_id")
            or ref.get("content_hash")
            or ref.get("commit")
            or ref.get("pr")
            or ref.get("url")
        )
        evidence_type = _normalize_evidence_type(ref.get("kind") or ref.get("type") or ref.get("source_type") or default_type)
    else:
        evidence_ref = ref
        evidence_type = _infer_evidence_type(str(ref or ""), default_type=default_type)
    text = public_safe_text(str(evidence_ref or ""), max_chars=240)
    if not text:
        return None
    return DocumentEvidenceEdge(
        document_path=document_path,
        evidence_type=evidence_type,
        evidence_ref=text,
        confidence=confidence,
    )


def _normalize_evidence_type(value: Any) -> DocumentEvidenceType:
    text = str(value or "").lower()
    if text in {"session", "transcript", "conversation"}:
        return "session"
    if text in {"commit", "git_commit"}:
        return "commit"
    if text in {"pull_request", "pr", "github_pr"}:
        return "pull_request"
    if text in {"live", "runtime", "smoke"}:
        return "live"
    if text in {"memory_card", "card"}:
        return "memory_card"
    if text in {"source_ref", "source"}:
        return "source_ref"
    return "unknown"


def _infer_evidence_type(value: str, *, default_type: DocumentEvidenceType) -> DocumentEvidenceType:
    text = str(value or "").lower()
    if text.startswith("session:") or text.startswith("turn-"):
        return "session"
    if text.startswith("commit:"):
        return "commit"
    if text.startswith("pr:") or text.startswith("pull_request:"):
        return "pull_request"
    if text.startswith("runtime:") or text.startswith("live:"):
        return "live"
    return default_type


def _dedupe_edges(edges: list[DocumentEvidenceEdge]) -> list[DocumentEvidenceEdge]:
    seen: set[tuple[str, str, str]] = set()
    result: list[DocumentEvidenceEdge] = []
    for edge in edges:
        key = (edge.document_path, edge.evidence_type, edge.evidence_ref)
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return result


def safe_authority_ref(value: Any) -> str:
    text = public_safe_text(str(value or ""), max_chars=240)
    if not text:
        return ""
    try:
        ensure_public_safe(text, "authority_ref")
    except ValueError:
        return f"redacted_ref:{short_hash(text)}"
    return text
