"""Qdrant + Docling searchable mirror PoC.

This module is deliberately behind optional imports. The default worker runtime
must not import Qdrant or Docling just because ``agent_knowledge.rag_ingress`` is
imported.

Boundary:
- Qdrant is a searchable runtime mirror only.
- Docling normalizes document text for that mirror only.
- CouchDB / ledger PG / Neo4j remain canonical authorities.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_knowledge.llm_brain_core._util import (
    ensure_public_safe,
    hash_payload,
    public_safe_text,
    require_sha256,
)
from agent_knowledge.redaction import redact_public_ingress_text

from .index_backend import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from .rag_ready_document import RagReadyDocument, assert_no_secret_like_metadata
from .server_runtime import public_ingress_leak_violations


DEFAULT_COLLECTION_NAME = "neurons_searchable_mirror_poc"
DEFAULT_VECTOR_SIZE = 64
EVIDENCE_PACKET_SCHEMA = "agent_knowledge_searchable_mirror_gate_evidence.v1"
MIRROR_AUTHORITY = "searchable_runtime_mirror"
MIRROR_BACKEND = "qdrant_docling"


class SearchableMirrorUnavailable(RuntimeError):
    """Raised when optional PoC dependencies are unavailable."""


class MarkdownNormalizer(Protocol):
    def normalize(self, document: RagReadyDocument) -> str: ...


class EmbeddingProvider(Protocol):
    @property
    def size(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class SearchableMirrorHit:
    result_type: str
    authority: str
    target_profile: str
    source_ref: str
    content_hash: str
    score: float | None
    summary: str
    canonical_resolution_required: bool = True
    authority_join_status: str = "not_checked"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "result_type": self.result_type,
            "authority": self.authority,
            "target_profile": self.target_profile,
            "source_ref": self.source_ref,
            "content_hash": self.content_hash,
            "summary": self.summary,
            "canonical_resolution_required": self.canonical_resolution_required,
            "authority_join_status": self.authority_join_status,
        }
        if self.score is not None:
            data["score"] = self.score
        ensure_public_safe(data, "SearchableMirrorHit")
        return data


class PassthroughMarkdownNormalizer:
    """Test/local fallback normalizer for already-redacted markdown."""

    def normalize(self, document: RagReadyDocument) -> str:
        return str(document.body or "")


class DoclingMarkdownNormalizer:
    """Normalize string input through Docling and export markdown."""

    def normalize(self, document: RagReadyDocument) -> str:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter
        except ImportError as exc:  # pragma: no cover - exercised only without optional extra
            raise SearchableMirrorUnavailable(
                "Docling is not installed; install the searchable mirror optional dependencies"
            ) from exc

        content_type = str(document.content_type or "").lower()
        if "html" in content_type or document.filename.lower().endswith((".html", ".htm")):
            fmt = InputFormat.HTML
        elif "markdown" in content_type or document.filename.lower().endswith((".md", ".markdown")):
            fmt = InputFormat.MD
        else:
            return str(document.body or "")

        result = DocumentConverter().convert_string(
            str(document.body or ""),
            format=fmt,
            name=document.filename,
        )
        return str(result.document.export_to_markdown())


class HashEmbeddingProvider:
    """Deterministic local embedding for tests and local smoke, not quality search."""

    def __init__(self, size: int = DEFAULT_VECTOR_SIZE) -> None:
        if int(size) <= 0:
            raise ValueError("embedding size must be positive")
        self._size = int(size)

    @property
    def size(self) -> int:
        return self._size

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self._size
        tokens = [token for token in str(text or "").lower().split() if token]
        if not tokens:
            tokens = [str(text or "empty")]
        for token in tokens:
            digest = uuid.uuid5(uuid.NAMESPACE_URL, token).int
            index = digest % self._size
            sign = 1.0 if (digest >> 8) % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class QdrantDoclingMirrorAdapter:
    """``IndexBackendAdapter`` implementation over Qdrant.

    ``client`` is a qdrant-client-shaped object. Tests inject a fake client so no
    network or optional dependency is required for the contract checks.
    """

    def __init__(
        self,
        *,
        client: Any,
        collection_name: str = DEFAULT_COLLECTION_NAME,
        normalizer: MarkdownNormalizer | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        ensure_collection: bool = True,
    ) -> None:
        self._client = client
        self._collection_name = str(collection_name or DEFAULT_COLLECTION_NAME)
        self._normalizer = normalizer or DoclingMarkdownNormalizer()
        self._embedding = embedding_provider or HashEmbeddingProvider()
        if ensure_collection:
            self._ensure_collection()

    def submit_document(
        self,
        document: RagReadyDocument,
        *,
        on_step_complete=None,
    ) -> BackendSubmitResult:
        markdown = self._normalizer.normalize(document)
        safe_markdown = _validate_mirror_text(markdown)
        vector = self._embedding.embed(safe_markdown)
        if len(vector) != self._embedding.size:
            raise ValueError("embedding provider returned wrong vector size")
        payload = _payload_for_document(document, safe_markdown)
        point_id = point_id_for_natural_key(
            target_profile=document.target_profile,
            idempotency_key=document.idempotency_key,
            content_hash=document.content_hash,
        )
        self._client.upsert(
            collection_name=self._collection_name,
            points=[_point_struct(point_id=point_id, vector=vector, payload=payload)],
        )
        if on_step_complete is not None:
            on_step_complete("qdrant_upsert", document_ref=point_id)
        return BackendSubmitResult(
            dataset_ref=_dataset_ref(self._collection_name),
            document_ref=point_id,
            status=IndexStatus.INDEXED,
        )

    def find_by_natural_key(
        self,
        *,
        target_profile: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> BackendDocumentHandle | None:
        if not target_profile or not idempotency_key or not payload_hash:
            return None
        point_id = point_id_for_natural_key(
            target_profile=target_profile,
            idempotency_key=idempotency_key,
            content_hash=payload_hash,
        )
        points = self._retrieve_points([point_id])
        if not points:
            return None
        payload = _point_payload(points[0])
        if (
            payload.get("target_profile") == target_profile
            and payload.get("idempotency_key") == idempotency_key
            and payload.get("content_hash") == payload_hash
        ):
            return BackendDocumentHandle(
                dataset_ref=_dataset_ref(self._collection_name),
                document_ref=point_id,
            )
        return None

    def document_status(self, handle: BackendDocumentHandle) -> str:
        return self.document_status_detail(handle).status

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail:
        if handle.dataset_ref != _dataset_ref(self._collection_name):
            return BackendStatusDetail(
                status=IndexStatus.UNKNOWN,
                progress=0.0,
                backend_raw_status="qdrant_collection_mismatch",
            )
        points = self._retrieve_points([handle.document_ref])
        if not points:
            return BackendStatusDetail(
                status=IndexStatus.UNKNOWN,
                progress=0.0,
                backend_raw_status="qdrant_not_found",
            )
        return BackendStatusDetail(
            status=IndexStatus.INDEXED,
            progress=1.0,
            backend_raw_status="qdrant_exists",
        )

    def query_documents(
        self,
        query: str,
        *,
        target_profile: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for mirror-candidate search.

        Results are not canonical read results. Callers must resolve them against
        ledger/CouchDB/Neo4j authorities before product use.
        """

        return self.query_mirror_candidates(query, target_profile=target_profile, limit=limit)

    def query_mirror_candidates(
        self,
        query: str,
        *,
        target_profile: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        safe_query = _validate_mirror_text(query)
        vector = self._embedding.embed(safe_query)
        if len(vector) != self._embedding.size:
            raise ValueError("embedding provider returned wrong vector size")
        result = _query_points(
            self._client,
            collection_name=self._collection_name,
            query=vector,
            limit=max(1, min(int(limit), 20)),
            query_filter=_target_profile_filter(target_profile),
        )
        hits: list[dict[str, Any]] = []
        for point in _query_result_points(result):
            payload = _point_payload(point)
            if target_profile and payload.get("target_profile") != target_profile:
                continue
            hits.append(_hit_from_payload(payload, score=_point_score(point)).to_dict())
        return hits

    def _ensure_collection(self) -> None:
        exists = False
        if hasattr(self._client, "collection_exists"):
            exists = bool(self._client.collection_exists(self._collection_name))
        else:
            try:
                self._client.get_collection(self._collection_name)
                exists = True
            except Exception as exc:
                if not _is_qdrant_not_found_error(exc):
                    raise SearchableMirrorUnavailable("Qdrant collection probe failed") from exc
                exists = False
        if exists:
            return
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=_vector_params(self._embedding.size),
        )

    def _retrieve_points(self, ids: list[str]) -> list[Any]:
        return list(
            self._client.retrieve(
                collection_name=self._collection_name,
                ids=ids,
                with_payload=True,
                with_vectors=False,
            )
        )


def build_local_qdrant_docling_mirror_adapter(
    *,
    path: str | None = None,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding_provider: EmbeddingProvider | None = None,
    normalizer: MarkdownNormalizer | None = None,
) -> QdrantDoclingMirrorAdapter:
    """Create a local Qdrant adapter using qdrant-client local mode."""

    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise SearchableMirrorUnavailable(
            "qdrant-client is not installed; install the searchable mirror optional dependencies"
        ) from exc
    client = QdrantClient(path=path) if path else QdrantClient(":memory:")
    return QdrantDoclingMirrorAdapter(
        client=client,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        normalizer=normalizer,
    )


def build_searchable_mirror_gate_report(
    *,
    dry_run: bool,
    redact_paths: bool,
    evidence_packet_path: str | Path | None = None,
    evidence_packet: dict[str, Any] | None = None,
    dual_write_evidence: bool = False,
    read_compare_evidence: bool = False,
    apple_silicon_local_evidence: bool = False,
    ubuntu_host_evidence: bool = False,
    operator_approval: bool = False,
) -> dict[str, Any]:
    if not dry_run:
        raise ValueError("searchable-mirror-gate requires --dry-run")
    if not redact_paths:
        raise ValueError("searchable-mirror-gate requires --redact-paths")

    evidence = _validate_searchable_mirror_evidence_packet(
        evidence_packet_path=evidence_packet_path,
        evidence_packet=evidence_packet,
    )
    blockers = list(evidence["blockers"])
    packet_valid = evidence["status"] == "valid"
    claimed = {
        "dual_write_evidence": bool(dual_write_evidence),
        "read_compare_evidence": bool(read_compare_evidence),
        "apple_silicon_local_evidence": bool(apple_silicon_local_evidence),
        "ubuntu_host_evidence": bool(ubuntu_host_evidence),
        "operator_approval": bool(operator_approval),
    }
    report = {
        "schema_version": "agent_knowledge_searchable_mirror_gate.v1",
        "scope": "searchable_mirror_replacement_poc",
        "candidate_backend": MIRROR_BACKEND,
        "dry_run": True,
        "redacted_paths": True,
        "network_used": False,
        "mutation_performed": False,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "canonical_authority": {
            "couchdb_transcript_source": "preserved",
            "ledger_pg_memory_cards": "preserved",
            "neo4j_ontology_graph": "preserved",
        },
        "claimed_evidence": claimed,
        "evidence_packet_status": evidence["status"],
        "evidence_packet_digest": evidence["packet_digest"],
        "evidence_packet_summary": evidence["summary"],
        "dual_write_status": _evidence_section_status(packet_valid, dual_write_evidence),
        "read_compare_status": _evidence_section_status(packet_valid, read_compare_evidence),
        "apple_silicon_local_first_status": _evidence_section_status(
            packet_valid,
            apple_silicon_local_evidence,
        ),
        "ubuntu_host_validation_status": _evidence_section_status(
            packet_valid,
            ubuntu_host_evidence,
        ),
        "production_authority_status": "NO-GO",
        "comparison_gate_status": (
            "ready_for_operator_cutover_packet"
            if packet_valid
            else "blocked_until_valid_evidence_packet"
        ),
        "ragflow_failover_status": "blocked_no_live_failover_from_searchable_mirror_gate",
        "blockers": blockers,
        "approval_required_before_live_mutation": True,
    }
    ensure_public_safe(report, "searchable_mirror_gate_report")
    return report


def point_id_for_natural_key(*, target_profile: str, idempotency_key: str, content_hash: str) -> str:
    key = f"{target_profile}\n{idempotency_key}\n{content_hash}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"neurons:{MIRROR_BACKEND}:{key}"))


def _validate_mirror_text(text: str) -> str:
    raw = str(text or "")
    leaks = public_ingress_leak_violations(raw)
    if leaks:
        raise ValueError("searchable mirror text still contains private content")
    safe = redact_public_ingress_text(raw)
    ensure_public_safe(safe, "searchable_mirror_text")
    return safe


def _payload_for_document(document: RagReadyDocument, markdown: str) -> dict[str, Any]:
    metadata = _public_safe_metadata(dict(document.metadata or {}))
    payload = {
        "authority": MIRROR_AUTHORITY,
        "backend": MIRROR_BACKEND,
        "target_profile": document.target_profile,
        "document_kind": document.document_kind,
        "artifact_kind": document.artifact_kind,
        "content_hash": document.content_hash,
        "idempotency_key": document.idempotency_key,
        "source_namespace": public_safe_text(document.source_namespace, max_chars=160),
        "source_alias": public_safe_text(document.source_alias, max_chars=240),
        "privacy_class": public_safe_text(document.privacy_class, max_chars=80),
        "content_type": public_safe_text(document.content_type, max_chars=120),
        "redaction_version": public_safe_text(document.redaction_version, max_chars=120),
        "text": markdown,
        "summary": public_safe_text(markdown, max_chars=512),
        "metadata": metadata,
    }
    ensure_public_safe(payload, "qdrant_docling_payload")
    return payload


def _public_safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    _assert_no_secret_like_metadata_tree(metadata)
    return {str(key): _public_safe_value(value) for key, value in metadata.items()}


def _assert_no_secret_like_metadata_tree(value: Any) -> None:
    if isinstance(value, dict):
        assert_no_secret_like_metadata(value)
        for item in value.values():
            _assert_no_secret_like_metadata_tree(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_secret_like_metadata_tree(item)


def _public_safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return public_safe_text(value, max_chars=512)
    if isinstance(value, dict):
        return {str(key): _public_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_safe_value(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return public_safe_text(str(value), max_chars=512)


def _dataset_ref(collection_name: str) -> str:
    return f"qdrant:{collection_name}"


def _vector_params(size: int) -> Any:
    try:
        from qdrant_client import models
    except ImportError:
        return {"size": size, "distance": "Cosine"}
    return models.VectorParams(size=size, distance=models.Distance.COSINE)


def _point_struct(*, point_id: str, vector: list[float], payload: dict[str, Any]) -> Any:
    try:
        from qdrant_client import models
    except ImportError:
        return {"id": point_id, "vector": vector, "payload": payload}
    return models.PointStruct(id=point_id, vector=vector, payload=payload)


def _query_points(
    client: Any,
    *,
    collection_name: str,
    query: list[float],
    limit: int,
    query_filter: Any,
) -> Any:
    kwargs = {
        "collection_name": collection_name,
        "query": query,
        "limit": limit,
    }
    if query_filter is not None:
        kwargs["query_filter"] = query_filter
    try:
        return client.query_points(**kwargs)
    except TypeError as exc:
        if query_filter is None or "query_filter" not in str(exc):
            raise
        kwargs.pop("query_filter", None)
        kwargs["filter"] = query_filter
        return client.query_points(**kwargs)


def _target_profile_filter(target_profile: str) -> Any:
    if not target_profile:
        return None
    try:
        from qdrant_client import models
    except ImportError:
        return {
            "must": [
                {
                    "key": "target_profile",
                    "match": {"value": str(target_profile)},
                }
            ]
        }
    return models.Filter(
        must=[
            models.FieldCondition(
                key="target_profile",
                match=models.MatchValue(value=str(target_profile)),
            )
        ]
    )


def _point_payload(point: Any) -> dict[str, Any]:
    if isinstance(point, dict):
        return dict(point.get("payload") or {})
    return dict(getattr(point, "payload", None) or {})


def _point_score(point: Any) -> float | None:
    if isinstance(point, dict):
        value = point.get("score")
    else:
        value = getattr(point, "score", None)
    return None if value is None else float(value)


def _query_result_points(result: Any) -> list[Any]:
    if isinstance(result, dict):
        points = result.get("points")
        if points is not None:
            return list(points)
        nested_result = result.get("result")
        if isinstance(nested_result, dict):
            return list(nested_result.get("points") or [])
        return list(nested_result or [])
    return list(getattr(result, "points", None) or [])


def _is_qdrant_not_found_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if status_code == 404 or response_status == 404:
        return True
    class_name = exc.__class__.__name__.lower()
    if "notfound" in class_name or "not_found" in class_name:
        return True
    message = str(exc).lower()
    return "404" in message or "not found" in message


def _hit_from_payload(payload: dict[str, Any], *, score: float | None) -> SearchableMirrorHit:
    return SearchableMirrorHit(
        result_type="searchable_mirror",
        authority=MIRROR_AUTHORITY,
        target_profile=str(payload.get("target_profile") or ""),
        source_ref=str(payload.get("idempotency_key") or ""),
        content_hash=str(payload.get("content_hash") or ""),
        score=score,
        summary=public_safe_text(str(payload.get("summary") or ""), max_chars=512),
    )


def _validate_searchable_mirror_evidence_packet(
    *,
    evidence_packet_path: str | Path | None,
    evidence_packet: dict[str, Any] | None,
) -> dict[str, Any]:
    if evidence_packet_path and evidence_packet is not None:
        raise ValueError("searchable-mirror-gate accepts either evidence packet path or object")
    if evidence_packet_path:
        try:
            evidence_packet = json.loads(Path(evidence_packet_path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("searchable mirror evidence packet not found") from exc
        except OSError as exc:
            raise ValueError("searchable mirror evidence packet could not be read") from exc
        except json.JSONDecodeError as exc:
            raise ValueError("searchable mirror evidence packet must be valid JSON") from exc
    if evidence_packet is None:
        return _invalid_evidence(["evidence_packet_required"])
    if not isinstance(evidence_packet, dict):
        return _invalid_evidence(["evidence_packet_must_be_object"])

    blockers: list[str] = []
    if evidence_packet.get("schema_version") != EVIDENCE_PACKET_SCHEMA:
        blockers.append("evidence_packet_schema_mismatch")
    try:
        ensure_public_safe(evidence_packet, "searchable_mirror_evidence_packet")
    except ValueError:
        blockers.append("evidence_packet_contains_private_content")

    dual_write = _section(evidence_packet, "dual_write", blockers)
    read_compare = _section(evidence_packet, "read_compare", blockers)
    apple_local = _section(evidence_packet, "apple_silicon_local", blockers)
    ubuntu_host = _section(evidence_packet, "ubuntu_host", blockers)
    approval = _section(evidence_packet, "operator_approval", blockers)

    _require_digest(dual_write, "dual_write.evidence_digest", blockers)
    target_profiles = dual_write.get("target_profiles") if isinstance(dual_write, dict) else None
    if not isinstance(target_profiles, list) or not target_profiles:
        blockers.append("dual_write.target_profiles_required")
    elif any(not isinstance(item, str) or not item.strip() for item in target_profiles):
        blockers.append("dual_write.target_profiles_invalid")

    _require_digest(read_compare, "read_compare.evidence_digest", blockers)
    compare_total = _non_negative_int(read_compare, "read_compare.total_count", blockers)
    compare_matched = _non_negative_int(read_compare, "read_compare.matched_count", blockers)
    compare_mismatch = _non_negative_int(read_compare, "read_compare.mismatch_count", blockers)
    if compare_mismatch is not None and compare_mismatch != 0:
        blockers.append("read_compare_mismatch_count_nonzero")
    if compare_total is not None and compare_matched is not None and compare_total != compare_matched:
        blockers.append("read_compare_total_matched_count_mismatch")

    _require_digest(apple_local, "apple_silicon_local.smoke_digest", blockers)
    _require_digest(ubuntu_host, "ubuntu_host.dry_run_digest", blockers)
    if not str(evidence_packet.get("collected_at") or "").strip():
        blockers.append("collected_at_required")
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        blockers.append("operator_approval_required_before_failover")
    _require_digest(approval, "operator_approval.approval_digest", blockers)

    if blockers:
        return _invalid_evidence(blockers)
    summary = {
        "schema_version": EVIDENCE_PACKET_SCHEMA,
        "target_profile_count": len(target_profiles),
        "read_compare_total_count": compare_total,
        "read_compare_mismatch_count": compare_mismatch,
        "operator_approval_status": "approved",
    }
    ensure_public_safe(summary, "searchable_mirror_evidence_summary")
    return {
        "status": "valid",
        "packet_digest": hash_payload(evidence_packet),
        "summary": summary,
        "blockers": [],
    }


def _invalid_evidence(blockers: list[str]) -> dict[str, Any]:
    return {
        "status": "invalid" if blockers != ["evidence_packet_required"] else "missing",
        "packet_digest": "",
        "summary": {},
        "blockers": blockers,
    }


def _section(packet: dict[str, Any], name: str, blockers: list[str]) -> dict[str, Any]:
    value = packet.get(name)
    if not isinstance(value, dict):
        blockers.append(f"{name}_section_required")
        return {}
    return value


def _require_digest(section: dict[str, Any], field: str, blockers: list[str]) -> None:
    if "." in field:
        key = field.split(".", 1)[1]
    else:
        key = field
    try:
        require_sha256(str(section.get(key) or ""), field)
    except ValueError:
        blockers.append(f"{field}_required")


def _non_negative_int(section: dict[str, Any], field: str, blockers: list[str]) -> int | None:
    key = field.split(".", 1)[1]
    value = section.get(key)
    if not isinstance(value, int) or value < 0:
        blockers.append(f"{field}_required")
        return None
    return value


def _evidence_section_status(packet_valid: bool, claimed: bool) -> str:
    if packet_valid:
        return "packet_valid"
    return "claimed_without_valid_packet" if claimed else "missing"


__all__ = [
    "DoclingMarkdownNormalizer",
    "EVIDENCE_PACKET_SCHEMA",
    "HashEmbeddingProvider",
    "MIRROR_AUTHORITY",
    "MIRROR_BACKEND",
    "PassthroughMarkdownNormalizer",
    "QdrantDoclingMirrorAdapter",
    "SearchableMirrorHit",
    "SearchableMirrorUnavailable",
    "build_local_qdrant_docling_mirror_adapter",
    "build_searchable_mirror_gate_report",
    "point_id_for_natural_key",
]
