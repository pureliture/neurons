from __future__ import annotations

import json
import math

import pytest

from agent_knowledge.rag_ingress.index_backend import BackendDocumentHandle, IndexStatus
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    EVIDENCE_PACKET_SCHEMA,
    HashEmbeddingProvider,
    MIRROR_AUTHORITY,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
    SearchableMirrorUnavailable,
    build_searchable_mirror_gate_report,
    point_id_for_natural_key,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document
from agent_knowledge.rag_ingress.state_cli import main as state_cli_main


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.created: list[tuple[str, object]] = []
        self.retrieve_calls: list[list[str]] = []

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collections[collection_name] = {}
        self.created.append((collection_name, vectors_config))

    def upsert(self, *, collection_name: str, points: list[object]) -> None:
        self.collections.setdefault(collection_name, {})
        for point in points:
            point_id = _point_field(point, "id")
            self.collections[collection_name][point_id] = {
                "id": point_id,
                "vector": _point_field(point, "vector"),
                "payload": _point_field(point, "payload"),
            }

    def retrieve(self, *, collection_name: str, ids: list[str], with_payload=True, with_vectors=False):
        _ = with_payload
        _ = with_vectors
        self.retrieve_calls.append(list(ids))
        points = self.collections.get(collection_name, {})
        return [points[item] for item in ids if item in points]

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter=None,
        filter=None,
    ):
        active_filter = query_filter if query_filter is not None else filter
        ranked = []
        for point in self.collections.get(collection_name, {}).values():
            if not _matches_filter(point, active_filter):
                continue
            score = _dot(point["vector"], query)
            ranked.append({**point, "score": score})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return {"points": ranked[:limit]}


class _ListQueryQdrantClient(_FakeQdrantClient):
    def query_points(self, **kwargs):
        return super().query_points(**kwargs)["points"]


class _StaticNormalizer:
    def __init__(self, text: str) -> None:
        self.text = text

    def normalize(self, document):
        _ = document
        return self.text


class _WrongSizeEmbedding:
    @property
    def size(self) -> int:
        return 8

    def embed(self, text: str) -> list[float]:
        _ = text
        return [1.0]


def test_qdrant_docling_adapter_upserts_normalized_markdown_and_reports_status():
    client = _FakeQdrantClient()
    document = _document(body="Ledger stays canonical. Mirror is search only.")
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=_StaticNormalizer("# Normalized\nLedger stays canonical."),
        embedding_provider=HashEmbeddingProvider(size=8),
    )

    result = adapter.submit_document(document)

    assert result.dataset_ref == "qdrant:mirror_test"
    assert result.status == IndexStatus.INDEXED
    stored = client.collections["mirror_test"][result.document_ref]
    assert stored["payload"]["authority"] == MIRROR_AUTHORITY
    assert stored["payload"]["target_profile"] == "ragflow-session-memory"
    assert stored["payload"]["content_hash"] == document.content_hash
    assert stored["payload"]["idempotency_key"] == document.idempotency_key
    assert stored["payload"]["text"] == "# Normalized\nLedger stays canonical."
    assert adapter.document_status_detail(
        BackendDocumentHandle(dataset_ref="qdrant:mirror_test", document_ref=result.document_ref)
    ).status == IndexStatus.INDEXED


def test_qdrant_docling_adapter_natural_key_is_exact_and_blank_keys_fail_closed():
    client = _FakeQdrantClient()
    document = _document()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )
    result = adapter.submit_document(document)

    handle = adapter.find_by_natural_key(
        target_profile=document.target_profile,
        idempotency_key=document.idempotency_key,
        payload_hash=document.content_hash,
    )

    assert handle == BackendDocumentHandle(dataset_ref="qdrant:mirror_test", document_ref=result.document_ref)
    assert adapter.find_by_natural_key(
        target_profile=document.target_profile,
        idempotency_key="",
        payload_hash=document.content_hash,
    ) is None
    assert adapter.find_by_natural_key(
        target_profile=document.target_profile,
        idempotency_key=document.idempotency_key,
        payload_hash="",
    ) is None
    expected_id = point_id_for_natural_key(
        target_profile=document.target_profile,
        idempotency_key=document.idempotency_key,
        content_hash=document.content_hash,
    )
    assert result.document_ref == expected_id


def test_qdrant_docling_adapter_query_returns_mirror_labeled_hits():
    client = _FakeQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )
    adapter.submit_document(_document(body="Qdrant mirror remains non authoritative."))
    adapter.submit_document(_document(target_profile="ragflow-project-memory", body="Other project mirror."))

    hits = adapter.query_documents("authoritative mirror", target_profile="ragflow-session-memory")

    assert len(hits) == 1
    assert hits[0]["authority"] == MIRROR_AUTHORITY
    assert hits[0]["result_type"] == "searchable_mirror"
    assert hits[0]["target_profile"] == "ragflow-session-memory"
    assert hits[0]["canonical_resolution_required"] is True
    assert hits[0]["authority_join_status"] == "not_checked"
    assert "score" in hits[0]


def test_qdrant_docling_adapter_filters_target_profile_before_limit():
    client = _FakeQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )
    adapter.submit_document(_document(target_profile="ragflow-project-memory", body="alpha beta gamma"))
    adapter.submit_document(_document(target_profile="ragflow-session-memory", body="alpha beta gamma"))

    hits = adapter.query_mirror_candidates("alpha beta gamma", target_profile="ragflow-session-memory", limit=1)

    assert len(hits) == 1
    assert hits[0]["target_profile"] == "ragflow-session-memory"


def test_qdrant_docling_adapter_accepts_list_query_response():
    client = _ListQueryQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )
    adapter.submit_document(_document(body="list response shape"))

    hits = adapter.query_mirror_candidates("list response shape", target_profile="ragflow-session-memory")

    assert len(hits) == 1
    assert hits[0]["target_profile"] == "ragflow-session-memory"


def test_qdrant_docling_adapter_query_checks_embedding_size():
    adapter = QdrantDoclingMirrorAdapter(
        client=_FakeQdrantClient(),
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=_WrongSizeEmbedding(),
    )

    with pytest.raises(ValueError, match="wrong vector size"):
        adapter.query_mirror_candidates("query")


def test_qdrant_docling_adapter_blocks_new_private_content_from_normalizer():
    client = _FakeQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=_StaticNormalizer("leak /Users/example/.codex/private/session.jsonl"),
        embedding_provider=HashEmbeddingProvider(size=8),
    )

    with pytest.raises(ValueError, match="private content"):
        adapter.submit_document(_document())

    assert client.collections["mirror_test"] == {}


def test_qdrant_docling_adapter_blocks_nested_secret_like_metadata():
    client = _FakeQdrantClient()
    adapter = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror_test",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )

    with pytest.raises(ValueError, match="secret-like metadata key rejected"):
        adapter.submit_document(_document(metadata={"nested": {"api_key": "redacted-but-key-is-secret"}}))


def test_qdrant_docling_adapter_does_not_create_collection_after_probe_error():
    class BoomClient:
        def __init__(self) -> None:
            self.created = False

        def get_collection(self, collection_name):
            _ = collection_name
            raise RuntimeError("transport unavailable")

        def create_collection(self, *, collection_name, vectors_config):
            _ = collection_name
            _ = vectors_config
            self.created = True

    client = BoomClient()

    with pytest.raises(SearchableMirrorUnavailable, match="collection probe failed"):
        QdrantDoclingMirrorAdapter(client=client)

    assert client.created is False


def test_qdrant_docling_adapter_wraps_collection_exists_probe_error():
    class BoomClient:
        def collection_exists(self, collection_name):
            _ = collection_name
            raise RuntimeError("auth failed")

        def create_collection(self, *, collection_name, vectors_config):
            _ = collection_name
            _ = vectors_config
            raise AssertionError("must not create after probe failure")

    with pytest.raises(SearchableMirrorUnavailable, match="collection probe failed"):
        QdrantDoclingMirrorAdapter(client=BoomClient())


def test_qdrant_docling_adapter_treats_string_404_as_collection_missing():
    class NotFound(Exception):
        status_code = "404"

    class Client:
        def __init__(self) -> None:
            self.created = False

        def get_collection(self, collection_name):
            _ = collection_name
            raise NotFound("not found")

        def create_collection(self, *, collection_name, vectors_config):
            _ = collection_name
            _ = vectors_config
            self.created = True

    client = Client()

    QdrantDoclingMirrorAdapter(client=client)

    assert client.created is True


def test_searchable_mirror_gate_blocks_failover_without_evidence_packet():
    report = build_searchable_mirror_gate_report(dry_run=True, redact_paths=True)

    assert report["production_authority_status"] == "NO-GO"
    assert report["comparison_gate_status"] == "blocked_until_valid_evidence_packet"
    assert report["ragflow_failover_status"] == "blocked_no_live_failover_from_searchable_mirror_gate"
    assert "evidence_packet_required" in report["blockers"]
    assert report["canonical_authority"]["ledger_pg_memory_cards"] == "preserved"

    ready = build_searchable_mirror_gate_report(
        dry_run=True,
        redact_paths=True,
        dual_write_evidence=True,
        read_compare_evidence=True,
        apple_silicon_local_evidence=True,
        ubuntu_host_evidence=True,
        operator_approval=True,
    )

    assert ready["blockers"] == ["evidence_packet_required"]
    assert ready["production_authority_status"] == "NO-GO"
    assert ready["comparison_gate_status"] == "blocked_until_valid_evidence_packet"
    assert ready["dual_write_status"] == "claimed_without_valid_packet"


def test_searchable_mirror_gate_accepts_valid_packet_without_live_failover():
    report = build_searchable_mirror_gate_report(
        dry_run=True,
        redact_paths=True,
        evidence_packet=_valid_evidence_packet(),
    )

    assert report["blockers"] == []
    assert report["evidence_packet_status"] == "valid"
    assert report["evidence_packet_digest"].startswith("sha256:")
    assert report["comparison_gate_status"] == "ready_for_operator_cutover_packet"
    assert report["production_authority_status"] == "NO-GO"
    assert report["ragflow_failover_status"] == "blocked_no_live_failover_from_searchable_mirror_gate"


def test_searchable_mirror_gate_rejects_compare_mismatch():
    packet = _valid_evidence_packet()
    packet["read_compare"]["mismatch_count"] = 1

    report = build_searchable_mirror_gate_report(
        dry_run=True,
        redact_paths=True,
        evidence_packet=packet,
    )

    assert report["comparison_gate_status"] == "blocked_until_valid_evidence_packet"
    assert "read_compare_mismatch_count_nonzero" in report["blockers"]


def test_searchable_mirror_gate_requires_non_empty_compare_evidence():
    packet = _valid_evidence_packet()
    packet["dual_write"]["total_count"] = 0
    packet["read_compare"]["total_count"] = 0
    packet["read_compare"]["matched_count"] = 0

    report = build_searchable_mirror_gate_report(
        dry_run=True,
        redact_paths=True,
        evidence_packet=packet,
    )

    assert report["comparison_gate_status"] == "blocked_until_valid_evidence_packet"
    assert "dual_write.total_count_must_be_positive" in report["blockers"]
    assert "read_compare.total_count_must_be_positive" in report["blockers"]


def test_searchable_mirror_gate_rejects_bool_counts():
    packet = _valid_evidence_packet()
    packet["read_compare"]["mismatch_count"] = False

    report = build_searchable_mirror_gate_report(
        dry_run=True,
        redact_paths=True,
        evidence_packet=packet,
    )

    assert report["comparison_gate_status"] == "blocked_until_valid_evidence_packet"
    assert "read_compare.mismatch_count_required" in report["blockers"]


def test_searchable_mirror_gate_cli_is_dry_run_redacted_and_no_go(tmp_path, capsys):
    evidence_packet = tmp_path / "evidence.json"
    evidence_packet.write_text(json.dumps(_valid_evidence_packet()), encoding="utf-8")
    rc = state_cli_main([
        "searchable-mirror-gate",
        "--redact-paths",
        "--dry-run",
        "--evidence-packet",
        str(evidence_packet),
    ])

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["dry_run"] is True
    assert report["network_used"] is False
    assert report["mutation_performed"] is False
    assert report["raw_paths_printed"] is False
    assert report["production_authority_status"] == "NO-GO"
    assert report["comparison_gate_status"] == "ready_for_operator_cutover_packet"
    assert report["ragflow_failover_status"] == "blocked_no_live_failover_from_searchable_mirror_gate"


def test_searchable_mirror_gate_cli_rejects_live_mode(capsys):
    rc = state_cli_main(["searchable-mirror-gate", "--redact-paths"])

    assert rc == 2
    assert "requires --dry-run" in capsys.readouterr().err


def _document(
    *,
    target_profile="ragflow-session-memory",
    body="Qdrant Docling mirror PoC.",
    metadata=None,
):
    return build_rag_ready_document(
        target_profile=target_profile,
        document_kind="conversation_chunk",
        source_namespace="codex",
        source_alias="session.md",
        privacy_class="private",
        body=body,
        filename="session.md",
        metadata=metadata or {"project": "neurons", "source_ref_id": "src_test"},
    )


def _point_field(point: object, name: str):
    if isinstance(point, dict):
        return point[name]
    return getattr(point, name)


def _dot(left: list[float], right: list[float]) -> float:
    assert len(left) == len(right)
    return sum(a * b for a, b in zip(left, right, strict=False)) / (
        math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)) or 1.0
    )


def _matches_filter(point: dict, query_filter) -> bool:
    if query_filter is None:
        return True
    payload = point["payload"]
    must = query_filter.get("must") if isinstance(query_filter, dict) else getattr(query_filter, "must", None)
    for condition in must or []:
        key = condition.get("key") if isinstance(condition, dict) else getattr(condition, "key", "")
        match = condition.get("match") if isinstance(condition, dict) else getattr(condition, "match", None)
        expected = match.get("value") if isinstance(match, dict) else getattr(match, "value", None)
        if payload.get(key) != expected:
            return False
    return True


def _valid_evidence_packet() -> dict:
    return {
        "schema_version": EVIDENCE_PACKET_SCHEMA,
        "collected_at": "2026-06-21T00:00:00Z",
        "dual_write": {
            "evidence_digest": _digest("dual-write"),
            "target_profiles": ["ragflow-session-memory"],
            "total_count": 2,
        },
        "read_compare": {
            "evidence_digest": _digest("read-compare"),
            "total_count": 2,
            "matched_count": 2,
            "mismatch_count": 0,
        },
        "apple_silicon_local": {"smoke_digest": _digest("apple")},
        "ubuntu_host": {"dry_run_digest": _digest("ubuntu")},
        "operator_approval": {
            "approved": True,
            "approval_digest": _digest("approval"),
        },
    }


def _digest(label: str) -> str:
    return "sha256:" + (label.encode().hex() * 64)[:64]
