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

from agent_knowledge.public_safe_util import (
    ensure_public_safe,
    hash_payload,
    public_safe_text,
    require_sha256,
)
from agent_knowledge.redaction import redact_public_ingress_text
from agent_knowledge.qdrant_write_gateway_runtime import (
    FoundationDirectWriteContract,
    QdrantWriteActivation,
)

from .retired_index_bridge import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from .rag_ready_document import RagReadyDocument, assert_no_secret_like_metadata
from .server_runtime import public_ingress_leak_violations


DEFAULT_COLLECTION_NAME = "neurons_searchable_mirror_poc"
DEFAULT_VECTOR_SIZE = 64

# Keyword payload fields declared as Qdrant payload indexes at collection-create
# time so production filters do not full-scan. ``privacy_class`` is mandatory for
# safe multi-privacy retrieval.
PAYLOAD_INDEX_FIELDS = (
    "target_profile",
    "privacy_class",
    "result_type",
    "project",
    "provider",
    "session_id_hash",
    "memory_id",
    "content_hash",
    "idempotency_key",
    "document_kind",
    "redaction_version",
)
EVIDENCE_PACKET_SCHEMA = "agent_knowledge_searchable_mirror_gate_evidence.v1"
MIRROR_AUTHORITY = "searchable_runtime_mirror"
MIRROR_BACKEND = "qdrant_docling"
FOUNDATION_DIRECT_WRITE_CONTRACT = FoundationDirectWriteContract(
    activation=QdrantWriteActivation.FOUNDATION_DIRECT,
    phase="pr_c_foundation_compatibility",
    audit_status="pending",
    coverage_status="pending",
)


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
    privacy_class: str = ""
    memory_id: str = ""
    session_id_hash: str = ""
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
            "privacy_class": self.privacy_class,
            "canonical_resolution_required": self.canonical_resolution_required,
            "authority_join_status": self.authority_join_status,
        }
        # Additive: only surface memory_id / session_id_hash when present so
        # existing points/tests (without them) keep their exact dict shape.
        if self.memory_id:
            data["memory_id"] = self.memory_id
        if self.session_id_hash:
            # session_id_hash is the CouchDB authority-join key (projection_state).
            data["session_id_hash"] = self.session_id_hash
        if self.score is not None:
            data["score"] = self.score
        ensure_public_safe(data, "SearchableMirrorHit")
        return data


@dataclass(frozen=True)
class MirrorDeletionResult:
    """Outcome of a mirror point deletion.

    ``status`` is backend-neutral: ``deleted`` (a point existed and was removed),
    ``absent`` (no point existed for the key; delete is a safe no-op), or
    ``collection_mismatch`` (the handle does not belong to this collection, so no
    deletion was attempted). ``existed`` reports whether a point was present
    *before* the delete so a GC caller can record a real removal vs. a no-op.
    """

    status: str
    document_ref: str
    existed: bool

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "document_ref": self.document_ref,
            "existed": self.existed,
        }
        ensure_public_safe(data, "MirrorDeletionResult")
        return data


class MirrorDeletionCapable(Protocol):
    """Optional deletion capability layered on top of ``RetiredIndexBridgeAdapter``.

    The backend-neutral ``RetiredIndexBridgeAdapter`` protocol covers submit / find /
    status only; it has no delete. RetiredIndexBridge document deletion currently bypasses
    the adapter and goes through the GC ``hard_delete_documents`` chokepoint, so
    there is no neutral delete seam yet. This optional protocol is the draft seam
    a GC/retirement path would target for a Qdrant mirror. It is intentionally
    NOT wired into any live route here.
    """

    def delete_document(
        self, handle: BackendDocumentHandle, *, missing_ok: bool = True
    ) -> "MirrorDeletionResult": ...


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
    """``RetiredIndexBridgeAdapter`` implementation over Qdrant.

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
        write_transport: Any | None = None,
        direct_write_contract: FoundationDirectWriteContract | None = None,
        ensure_collection: bool = False,
    ) -> None:
        self._client = client
        self._collection_name = str(collection_name or DEFAULT_COLLECTION_NAME)
        if write_transport is not None and direct_write_contract is not None:
            raise ValueError("qdrant_write_transport_conflict")
        if direct_write_contract is not None:
            if not isinstance(direct_write_contract, FoundationDirectWriteContract):
                raise ValueError("foundation_direct_contract_invalid")
            from agent_knowledge.qdrant_write_gateway_runtime import (
                DEFAULT_QDRANT_MARKER_COLLECTION,
                DirectQdrantWriteTransport,
                QdrantCollectionPolicy,
            )

            policy = QdrantCollectionPolicy(
                product_collections=(self._collection_name,),
                marker_collection=DEFAULT_QDRANT_MARKER_COLLECTION,
            )
            write_transport = DirectQdrantWriteTransport(
                client=client,
                collection_name=self._collection_name,
                policy=policy,
            )
        self._write_transport = write_transport
        self._normalizer = normalizer or DoclingMarkdownNormalizer()
        self._embedding = embedding_provider or HashEmbeddingProvider()
        if ensure_collection:
            raise SearchableMirrorUnavailable("implicit_collection_provisioning_disabled")

    def submit_document(
        self,
        document: RagReadyDocument,
        *,
        on_step_complete=None,
    ) -> BackendSubmitResult:
        if self._write_transport is None:
            raise SearchableMirrorUnavailable("qdrant_write_transport_required")
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
        self._write_transport.upsert_points(
            points=[_point_struct(point_id=point_id, vector=vector, payload=payload)]
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
        privacy_class: str = "",
        filters: dict[str, str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for mirror-candidate search.

        Results are not canonical read results. Callers must resolve them against
        ledger/CouchDB/Neo4j authorities before product use.
        """

        return self.query_mirror_candidates(
            query,
            target_profile=target_profile,
            privacy_class=privacy_class,
            filters=filters,
            limit=limit,
        )

    def query_mirror_candidates(
        self,
        query: str,
        *,
        target_profile: str = "",
        privacy_class: str = "",
        filters: dict[str, str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        safe_query = _validate_mirror_text(query)
        vector = self._embedding.embed(safe_query)
        if len(vector) != self._embedding.size:
            raise ValueError("embedding provider returned wrong vector size")
        merged_filters = dict(filters or {})
        if privacy_class:
            merged_filters["privacy_class"] = privacy_class
        conditions = _filter_conditions_dict(target_profile=target_profile, filters=merged_filters)
        # Fail-closed: never run a fully-unscoped query (it would return the entire
        # collection across all profiles/privacy classes). The caller must scope by
        # target_profile and/or privacy_class/filters.
        if not conditions:
            raise ValueError(
                "mirror query requires a scoping condition (target_profile and/or privacy_class/filters)"
            )
        result = _query_points(
            self._client,
            collection_name=self._collection_name,
            query=vector,
            limit=max(1, min(int(limit), 20)),
            query_filter=_payload_filter(conditions),
        )
        hits: list[dict[str, Any]] = []
        for point in _query_result_points(result):
            payload = _point_payload(point)
            # Defence-in-depth: re-check every condition against the returned
            # payload so a backend that ignores the filter cannot leak a row
            # from another profile/privacy_class.
            if not _payload_satisfies(payload, conditions):
                continue
            hits.append(_hit_from_payload(payload, score=_point_score(point)).to_dict())
        return hits

    def close(self) -> None:
        """Close live network clients held by the adapter when supported."""

        for resource in (self._client, self._embedding):
            closer = getattr(resource, "close", None)
            if not callable(closer):
                continue
            closer()

    def delete_document(
        self, handle: BackendDocumentHandle, *, missing_ok: bool = True
    ) -> MirrorDeletionResult:
        """Delete one mirror point by handle (draft GC/retirement seam).

        Idempotent: deleting an absent point is a safe no-op when ``missing_ok``.
        This never touches RetiredIndexBridge; it only removes a Qdrant point, and it is not
        wired into any live GC route -- it exists so a future GC chokepoint can
        target the same neutral seam for the mirror.
        """

        if self._write_transport is None:
            raise SearchableMirrorUnavailable("qdrant_write_transport_required")
        if handle.dataset_ref != _dataset_ref(self._collection_name):
            return MirrorDeletionResult(
                status="collection_mismatch",
                document_ref=handle.document_ref,
                existed=False,
            )
        existed = bool(self._retrieve_points([handle.document_ref]))
        if not existed and not missing_ok:
            raise ValueError("mirror point not found and missing_ok is False")
        self._write_transport.delete_points(
            points_selector=_points_selector([handle.document_ref]),
            item_count=1,
        )
        return MirrorDeletionResult(
            status="deleted" if existed else "absent",
            document_ref=handle.document_ref,
            existed=existed,
        )

    def delete_by_natural_key(
        self,
        *,
        target_profile: str,
        idempotency_key: str,
        content_hash: str,
        missing_ok: bool = True,
    ) -> MirrorDeletionResult:
        """Resolve a natural key to its deterministic point id, then delete it."""

        handle = self.find_by_natural_key(
            target_profile=target_profile,
            idempotency_key=idempotency_key,
            payload_hash=content_hash,
        )
        if handle is None:
            point_id = point_id_for_natural_key(
                target_profile=target_profile,
                idempotency_key=idempotency_key,
                content_hash=content_hash,
            )
            if not missing_ok:
                raise ValueError("mirror point not found and missing_ok is False")
            return MirrorDeletionResult(status="absent", document_ref=point_id, existed=False)
        return self.delete_document(handle, missing_ok=missing_ok)

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def collection_exists(self) -> bool | None:
        """Whether the backing collection exists, or None if unknowable.

        Read-only probe (no create). Supports both the fake (``collection_exists``)
        and the real client (``collection_exists`` / ``get_collection``). Returns
        None only when the client cannot answer, so a caller can decide its own
        fail-closed policy.
        """
        prober = getattr(self._client, "collection_exists", None)
        if callable(prober):
            try:
                return bool(prober(self._collection_name))
            except Exception:
                return None
        describe = getattr(self._client, "get_collection", None)
        if not callable(describe):
            return None
        try:
            describe(self._collection_name)
            return True
        except Exception as exc:
            if _is_qdrant_not_found_error(exc):
                return False
            return None

    @property
    def embedding_size(self) -> int:
        """Declared embedding vector size for this adapter's provider."""
        return int(self._embedding.size)

    def collection_vector_size(self) -> int | None:
        """Configured vector size of the backing collection, or None if unknown.

        Reads the size from the client without an embed/upsert. Supports the local
        fake (``collection_vector_size`` accessor) and the real qdrant-client
        (``get_collection`` -> ``config.params.vectors.size``). Returns None when
        the client cannot report it (a dim guard then falls back to provider/upsert
        validation).
        """
        getter = getattr(self._client, "collection_vector_size", None)
        if callable(getter):
            try:
                return getter(self._collection_name)
            except Exception:
                return None
        describe = getattr(self._client, "get_collection", None)
        if not callable(describe):
            return None
        try:
            info = describe(self._collection_name)
        except Exception:
            return None
        try:
            vectors = info.config.params.vectors  # type: ignore[attr-defined]
            size = getattr(vectors, "size", None)
            return int(size) if size is not None else None
        except Exception:
            return None

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
    write_transport: Any | None = None,
) -> QdrantDoclingMirrorAdapter:
    """Create a read-only local adapter unless an explicit transport is supplied."""

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
        write_transport=write_transport,
    )


def build_remote_qdrant_docling_mirror_adapter(
    *,
    url: str,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    embedding_provider: EmbeddingProvider | None = None,
    normalizer: MarkdownNormalizer | None = None,
    write_transport: Any | None = None,
    direct_write_contract: FoundationDirectWriteContract | None = None,
    ensure_collection: bool = False,
) -> QdrantDoclingMirrorAdapter:
    """Create a Qdrant adapter against a remote server URL (e.g. compose service).

    Optional-dependency guarded like the local builder. Used by the M6 dual-write
    activation when ``QDRANT_URL`` is configured; tests inject a fake client to the
    adapter directly and never exercise this network path.

    Collection creation is never implicit. Product writers must use the gateway
    builder and pass its typed transport; this legacy builder is read-only unless
    an explicit Foundation compatibility transport is supplied.
    """

    if not url:
        raise SearchableMirrorUnavailable("QDRANT_URL is required for the remote mirror adapter")
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise SearchableMirrorUnavailable(
            "qdrant-client is not installed; install the searchable mirror optional dependencies"
        ) from exc
    return QdrantDoclingMirrorAdapter(
        client=QdrantClient(url=url),
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        normalizer=normalizer,
        write_transport=write_transport,
        direct_write_contract=direct_write_contract,
        ensure_collection=ensure_collection,
    )


def build_remote_qdrant_docling_sidecar_adapter(
    *,
    read_url: str,
    read_api_key_path: str | Path,
    gateway_endpoint: str,
    gateway_token_path: str | Path,
    gateway_ca_path: str | Path,
    gateway_generation: int,
    collection_name: str,
    source: Any,
    embedding_provider: EmbeddingProvider | None = None,
    normalizer: MarkdownNormalizer | None = None,
) -> QdrantDoclingMirrorAdapter:
    """Build a read client plus the central sidecar HTTPS write transport.

    The writer process owns no Qdrant write credential and never constructs a
    Qdrant-backed marker/product mutation wrapper.
    """

    from agent_knowledge.qdrant_write_gateway_http import (
        RemoteQdrantGatewayTransport,
        read_projected_qdrant_api_key,
        validate_qdrant_read_base_url,
    )

    try:
        validated_read_url = validate_qdrant_read_base_url(read_url)
    except Exception:
        raise SearchableMirrorUnavailable("qdrant_read_url_invalid") from None
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise SearchableMirrorUnavailable(
            "qdrant-client is not installed; install the searchable mirror optional dependencies"
        ) from exc
    read_client = QdrantClient(
        url=validated_read_url,
        api_key=read_projected_qdrant_api_key(read_api_key_path),
        timeout=5,
        prefer_grpc=False,
        trust_env=False,
        follow_redirects=False,
    )
    transport = RemoteQdrantGatewayTransport(
        endpoint=gateway_endpoint,
        source=source,
        generation=gateway_generation,
        collection_name=collection_name,
        token_path=gateway_token_path,
        ca_path=gateway_ca_path,
    )
    return QdrantDoclingMirrorAdapter(
        client=read_client,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
        normalizer=normalizer,
        write_transport=transport,
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
        "index_failover_status": "blocked_no_live_failover_from_searchable_mirror_gate",
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
    # Promote RetiredIndexBridge-parity filter keys to top-level payload so they can be
    # indexed and filtered like target_profile (they otherwise live only inside
    # the nested ``metadata`` dict). None of these overwrite an existing top-level
    # key.
    payload.update(_promoted_filter_fields(metadata, document))
    ensure_public_safe(payload, "qdrant_docling_payload")
    return payload


def _promoted_filter_fields(metadata: dict[str, Any], document: RagReadyDocument) -> dict[str, Any]:
    # Canonical filter field name is ``result_type`` (RetiredIndexBridge uses result_type/type
    # interchangeably); fall back to the document_kind so the field is always set.
    result_type = str(metadata.get("result_type") or metadata.get("type") or document.document_kind or "")
    promoted: dict[str, Any] = {"result_type": public_safe_text(result_type, max_chars=120)}
    # ``memory_id`` is promoted so the downstream brain_query consumer
    # (``_mirror_memory_id``) can read it top-level for dedup/conflict-by-memory_id;
    # additive and backward-compatible (absent -> not set).
    for key in ("project", "provider", "session_id_hash", "memory_id"):
        value = metadata.get(key)
        if value is not None and str(value) != "":
            promoted[key] = public_safe_text(str(value), max_chars=240)
    return promoted


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


def _points_selector(ids: list[str]) -> Any:
    try:
        from qdrant_client import models
    except ImportError:
        return list(ids)
    return models.PointIdsList(points=list(ids))


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


def _filter_conditions_dict(*, target_profile: str, filters: dict[str, str] | None) -> dict[str, str]:
    conditions: dict[str, str] = {}
    if target_profile:
        conditions["target_profile"] = str(target_profile)
    for key, value in (filters or {}).items():
        key = str(key)
        if value is None or str(value) == "":
            continue
        # The explicit target_profile argument is authoritative: a filters entry
        # may not silently widen/redirect scope by overwriting it.
        if key == "target_profile" and target_profile and str(value) != str(target_profile):
            raise ValueError("filters.target_profile conflicts with the target_profile argument")
        conditions[key] = str(value)
    return conditions


def _payload_filter(conditions: dict[str, str]) -> Any:
    if not conditions:
        return None
    try:
        from qdrant_client import models
    except ImportError:
        return {
            "must": [{"key": key, "match": {"value": value}} for key, value in conditions.items()]
        }
    return models.Filter(
        must=[
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in conditions.items()
        ]
    )


def _payload_satisfies(payload: dict[str, Any], conditions: dict[str, str]) -> bool:
    return all(payload.get(key) == value for key, value in conditions.items())


def _target_profile_filter(target_profile: str) -> Any:
    # Retained single-field helper, now delegating to the general builder.
    return _payload_filter(_filter_conditions_dict(target_profile=target_profile, filters=None))


def _keyword_index_schema() -> Any:
    try:
        from qdrant_client import models
    except ImportError:
        return "keyword"
    return models.PayloadSchemaType.KEYWORD


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
    if isinstance(result, (list, tuple)):
        return list(result)
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
    if _is_status_404(status_code) or _is_status_404(response_status):
        return True
    class_name = exc.__class__.__name__.lower()
    if "notfound" in class_name or "not_found" in class_name:
        return True
    message = str(exc).lower()
    return "404" in message or "not found" in message


def _is_status_404(value: Any) -> bool:
    return value == 404 or str(value or "").strip() == "404"


def _hit_from_payload(payload: dict[str, Any], *, score: float | None) -> SearchableMirrorHit:
    return SearchableMirrorHit(
        result_type="searchable_mirror",
        authority=MIRROR_AUTHORITY,
        target_profile=str(payload.get("target_profile") or ""),
        source_ref=str(payload.get("idempotency_key") or ""),
        content_hash=str(payload.get("content_hash") or ""),
        score=score,
        summary=public_safe_text(str(payload.get("summary") or ""), max_chars=512),
        privacy_class=public_safe_text(str(payload.get("privacy_class") or ""), max_chars=80),
        memory_id=public_safe_text(str(payload.get("memory_id") or ""), max_chars=240),
        session_id_hash=public_safe_text(str(payload.get("session_id_hash") or ""), max_chars=240),
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
    _positive_int(dual_write, "dual_write.total_count", blockers)
    target_profiles = dual_write.get("target_profiles") if isinstance(dual_write, dict) else None
    if not isinstance(target_profiles, list) or not target_profiles:
        blockers.append("dual_write.target_profiles_required")
    elif any(not isinstance(item, str) or not item.strip() for item in target_profiles):
        blockers.append("dual_write.target_profiles_invalid")

    _require_digest(read_compare, "read_compare.evidence_digest", blockers)
    compare_total = _positive_int(read_compare, "read_compare.total_count", blockers)
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
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        blockers.append(f"{field}_required")
        return None
    return value


def _positive_int(section: dict[str, Any], field: str, blockers: list[str]) -> int | None:
    value = _non_negative_int(section, field, blockers)
    if value is not None and value <= 0:
        blockers.append(f"{field}_must_be_positive")
        return None
    return value


def _evidence_section_status(packet_valid: bool, claimed: bool) -> str:
    if packet_valid:
        return "packet_valid"
    return "claimed_without_valid_packet" if claimed else "missing"


__all__ = [
    "DoclingMarkdownNormalizer",
    "EVIDENCE_PACKET_SCHEMA",
    "FOUNDATION_DIRECT_WRITE_CONTRACT",
    "HashEmbeddingProvider",
    "MIRROR_AUTHORITY",
    "MIRROR_BACKEND",
    "MirrorDeletionCapable",
    "MirrorDeletionResult",
    "PAYLOAD_INDEX_FIELDS",
    "PassthroughMarkdownNormalizer",
    "QdrantDoclingMirrorAdapter",
    "SearchableMirrorHit",
    "SearchableMirrorUnavailable",
    "build_local_qdrant_docling_mirror_adapter",
    "build_remote_qdrant_docling_sidecar_adapter",
    "build_searchable_mirror_gate_report",
    "point_id_for_natural_key",
]
