from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ._util import ensure_public_safe, public_safe_text


@dataclass(frozen=True)
class DocumentBridgeResult:
    status: str
    authority: str = "external_document_bridge"
    evidence: tuple[dict[str, Any], ...] = ()
    details: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "DocumentBridgeResult")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [dict(item) for item in self.evidence]
        data["details"] = list(self.details)
        return data


class DocumentBridge(Protocol):
    def search_documents(self, *, query: str, project: str, limit: int = 5) -> DocumentBridgeResult: ...


class DisabledDocumentBridge:
    def search_documents(self, *, query: str, project: str, limit: int = 5) -> DocumentBridgeResult:
        _ = query
        _ = project
        _ = limit
        return DocumentBridgeResult(status="disabled", details=("not_part_of_core_read_path",))


class RetiredIndexBridgeDocumentBridge:
    """Read-only RetiredIndexBridge document/citation bridge.

    The bridge is deliberately search-only. It returns external document
    evidence with no canonical-memory authority and no write/delete methods.
    """

    def __init__(self, *, retired_index_bridge: Any, dataset_ids: list[str] | tuple[str, ...]) -> None:
        self._retired_index_bridge = retired_index_bridge
        self._dataset_ids = list(dataset_ids)

    def search_documents(self, *, query: str, project: str, limit: int = 5) -> DocumentBridgeResult:
        if not self._dataset_ids:
            return DocumentBridgeResult(status="disabled", details=("no_bridge_datasets",))
        bounded = max(1, min(int(limit), 20))
        try:
            chunks = self._retired_index_bridge.retrieve(
                query,
                self._dataset_ids,
                filters={"project": project} if project else None,
                limit=bounded,
            )
        except Exception as exc:
            return DocumentBridgeResult(status="unavailable", details=(type(exc).__name__,))
        evidence = tuple(_chunk_to_evidence(chunk) for chunk in chunks[:bounded] if isinstance(chunk, dict))
        return DocumentBridgeResult(
            status="available" if evidence else "available_empty",
            evidence=evidence,
            details=("index_read_only_bridge",),
        )


def _chunk_to_evidence(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    evidence = {
        "authority": "external_document_bridge",
        "result_type": public_safe_text(str(chunk.get("result_type") or metadata.get("result_type") or "document"), max_chars=80),
        "title": public_safe_text(str(chunk.get("title") or metadata.get("title") or ""), max_chars=240),
        "summary": public_safe_text(str(chunk.get("summary") or chunk.get("content") or ""), max_chars=512),
        "score": chunk.get("score"),
        "source_ref_id": public_safe_text(str(chunk.get("source_ref_id") or metadata.get("source_ref_id") or ""), max_chars=160),
    }
    ensure_public_safe(evidence, "RetiredIndexBridgeDocumentBridge.evidence")
    return evidence
