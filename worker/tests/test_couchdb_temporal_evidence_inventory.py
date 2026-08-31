"""Public contract tests for the read-only CouchDB temporal evidence inventory."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_knowledge.cli import COMMAND_HANDLERS, COMMAND_METADATA
from agent_knowledge.couchdb_source.document_model import (
    SourceDocType,
    build_coverage_hash,
    build_source_hash,
    build_source_revision_token,
    observed_time_bounds,
    sha256_hash,
    active_source_revision_pointer_doc_id,
    source_revision_manifest_doc_id,
    source_revision_member_doc_id,
)
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_revision import (
    _manifest_hash,
    _member_hash,
    _source_member_descriptor,
    _source_revision,
)
from agent_knowledge.couchdb_source.temporal_evidence_inventory import (
    DEFAULT_INDEX_DESIGN_DOCUMENT,
    DEFAULT_INDEX_NAME,
    _per_request_timeout_seconds,
    inventory_temporal_evidence,
    main,
)
from agent_knowledge.transport_contract import ProxyResponse


def test_cli_is_registered_as_read_only_inventory() -> None:
    command = "couchdb-temporal-evidence-inventory"

    assert command in COMMAND_HANDLERS
    assert COMMAND_METADATA[command] == {
        "runtime_category": "read_only",
        "deletion_candidate": False,
        "live_mutation_requires_approval": False,
    }


class _FakeCouchSource:
    def __init__(
        self,
        documents: list[dict],
        *,
        indexed: bool = True,
        sequences: list[str] | None = None,
        execution_stats: dict[str, object] | None = None,
        partial_filter_selector: object | None = None,
    ) -> None:
        self.documents = documents
        self.indexed = indexed
        self.sequences = list(sequences or ["sequence-before", "sequence-before"])
        self.find_calls: list[dict] = []
        self.explain_calls: list[dict] = []
        self.execution_stats = execution_stats or {
            "total_docs_examined": 1,
            "total_keys_examined": 1,
        }
        self.partial_filter_selector = partial_filter_selector

    def explain_find(self, **kwargs: object) -> dict:
        self.explain_calls.append(dict(kwargs))
        if not self.indexed:
            return {"index": {"type": "special", "name": "_all_docs", "def": {"fields": []}}}
        index = {
            "index": {
                "type": "json",
                "name": DEFAULT_INDEX_NAME,
                "ddoc": DEFAULT_INDEX_DESIGN_DOCUMENT,
                "def": {"fields": [{"project": "asc"}, {"doc_type": "asc"}]},
            }
        }
        if self.partial_filter_selector is not None:
            index["index"]["def"]["partial_filter_selector"] = self.partial_filter_selector
        return index

    def read_change_sequence(self) -> str:
        return self.sequences.pop(0) if len(self.sequences) > 1 else self.sequences[0]

    def find_by_type(self, doc_type: str, **kwargs: object) -> list[dict]:
        self.find_calls.append({"doc_type": doc_type, **kwargs})
        selector = dict(kwargs["selector"])
        matching = [
            document
            for document in self.documents
            if document.get("doc_type") == doc_type and document.get("project") == selector["project"]
        ]
        fields = list(kwargs["fields"])
        return [
            {field: document[field] for field in fields if field in document}
            for document in matching[: int(kwargs["limit"])]
        ]

    def find_by_type_with_execution_stats(self, doc_type: str, **kwargs: object) -> dict[str, object]:
        return {
            "documents": self.find_by_type(doc_type, **kwargs),
            "execution_stats": self.execution_stats,
        }


def _source_documents(*, chunk_temporal: tuple[str, str] = ("2026-07-01T01:00:00Z", "2026-07-01T02:00:00Z"), parent: tuple[str, str] = ("", ""), legacy: tuple[str, str] = ("", "")) -> list[dict]:
    session = sha256_hash("session-marker")
    documents = [
        {
            "_id": "private-session-id-marker",
            "_rev": "9-private-revision-marker",
            "doc_type": SourceDocType.TRANSCRIPT_SESSION,
            "project": "neurons",
            "session_id_hash": session,
            "observed_at_start": parent[0],
            "observed_at_end": parent[1],
            "started_at": legacy[0],
            "ended_at": legacy[1],
        },
        *[
            {
                "_id": f"private-{doc_type}-id-marker",
                "_rev": f"3-{doc_type}-revision-marker",
                "doc_type": doc_type,
                "project": "neurons",
                "session_id_hash": session,
                "observed_at_start": chunk_temporal[0],
                "observed_at_end": chunk_temporal[1],
                "body": "private body marker must never be requested or rendered",
                "materialized_at": "private materialized marker",
                "source_locator": "/private/locator/marker",
                "content_hash": sha256_hash(f"{doc_type}-content"),
                **(
                    {
                        "conversation_chunk_count": 1,
                        "tool_evidence_bundle_count": 1,
                    }
                    if doc_type == SourceDocType.COVERAGE_MANIFEST
                    else {}
                ),
            }
            for doc_type in (
                SourceDocType.CONVERSATION_CHUNK,
                SourceDocType.TOOL_EVIDENCE_BUNDLE,
                SourceDocType.COVERAGE_MANIFEST,
            )
        ],
    ]
    _refresh_manifest_hashes(documents)
    return documents


def _refresh_manifest_hashes(documents: list[dict]) -> None:
    """Make fixtures follow the public source document model without bodies."""

    chunks = [doc for doc in documents if doc["doc_type"] == SourceDocType.CONVERSATION_CHUNK]
    bundles = [doc for doc in documents if doc["doc_type"] == SourceDocType.TOOL_EVIDENCE_BUNDLE]
    sessions = [doc for doc in documents if doc["doc_type"] == SourceDocType.TRANSCRIPT_SESSION]
    for bundle in bundles:
        bundle["coverage_hash"] = build_coverage_hash([str(bundle["content_hash"])])
    observed_start, observed_end = observed_time_bounds(
        sessions=sessions,
        chunks=[*chunks, *bundles],
    )
    source_hash = build_source_hash(
        [str(chunk["content_hash"]) for chunk in chunks],
        [str(bundle["coverage_hash"]) for bundle in bundles],
        observed_at_start=observed_start,
        observed_at_end=observed_end,
        conversation_revision_tokens=[
            build_source_revision_token(chunk, material_hash_field="content_hash")
            for chunk in chunks
        ],
        tool_evidence_revision_tokens=[
            build_source_revision_token(bundle, material_hash_field="content_hash")
            for bundle in bundles
        ],
    )
    for manifest in (doc for doc in documents if doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST):
        manifest["conversation_coverage_hash"] = build_coverage_hash(
            [str(chunk["content_hash"]) for chunk in chunks]
        )
        manifest["tool_evidence_coverage_hash"] = build_coverage_hash(
            [str(bundle["coverage_hash"]) for bundle in bundles]
        )
        manifest["source_hash"] = source_hash


def _run_cli(store: _FakeCouchSource, argv: list[str]) -> tuple[int, dict]:
    output = StringIO()
    with (
        patch.dict(os.environ, {"COUCHDB_URL": "https://private-couch-marker.invalid"}),
        patch("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore", return_value=store),
        patch("sys.stdout", output),
    ):
        result = main(argv)
    return result, json.loads(output.getvalue())


def _argv(*, limit: int = 10) -> list[str]:
    return [
        "--project",
        "neurons",
        "--limit",
        str(limit),
        "--max-runtime-seconds",
        "10",
        "--require-complete-scan",
    ]


def test_cli_reports_complete_only_for_direct_valid_source_evidence_and_redacts_source_values() -> None:
    store = _FakeCouchSource(_source_documents())

    code, report = _run_cli(store, _argv())

    assert code == 0
    assert report["schema_version"] == "couchdb_temporal_evidence_inventory.v1"
    assert report["authority"] == "couchdb_source_native"
    assert report["status"] == "complete"
    assert report["runtime_category"] == "read_only"
    assert report["mutation_performed"] is False
    assert report["scan_exhausted"] is True
    assert report["direct_observed_at_valid_count"] == 2
    assert report["family_document_counts"] == {
        "conversation_chunk": 1,
        "coverage_manifest": 1,
        "tool_evidence_bundle": 1,
        "transcript_session": 1,
    }
    assert report["parent_observed_fallback_count"] == 0
    assert report["parent_legacy_fallback_count"] == 0
    assert report["per_family_limit"] == 10
    assert report["global_document_limit"] == 40
    assert report["execution_stats_summary"] == {
        "total_docs_examined": 6,
        "total_keys_examined": 6,
    }
    assert report["source_update_seq_start_hash"] == report["source_update_seq_end_hash"]
    assert report["source_update_seq_start_hash"].startswith("sha256:")
    rendered = json.dumps(report, sort_keys=True)
    for marker in ("private-session-id-marker", "private-revision-marker", "private body marker", "/private/locator", "private-couch-marker"):
        assert marker not in rendered
    assert {field for call in store.find_calls for field in call["fields"]}.isdisjoint({"body", "source_locator", "materialized_at"})
    coverage_call = next(call for call in store.find_calls if call["doc_type"] == SourceDocType.COVERAGE_MANIFEST)
    assert {"observed_at_start", "observed_at_end"}.issubset(coverage_call["fields"])


def test_missing_invalid_and_reversed_direct_evidence_requires_repair() -> None:
    documents = _source_documents()
    documents.append({**documents[1], "_id": "private-extra-chunk-id-marker"})
    manifest = next(doc for doc in documents if doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST)
    manifest["conversation_chunk_count"] = 2
    children = [
        doc
        for doc in documents
        if doc["doc_type"]
        in {SourceDocType.CONVERSATION_CHUNK, SourceDocType.TOOL_EVIDENCE_BUNDLE}
    ]
    children[0]["observed_at_start"] = ""
    children[0]["observed_at_end"] = ""
    children[1]["observed_at_start"] = "not-a-time"
    children[2]["observed_at_start"] = "2026-07-02T02:00:00Z"
    children[2]["observed_at_end"] = "2026-07-02T01:00:00Z"

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["status"] == "blocked"
    assert report["scan_exhausted"] is True
    assert report["repair_required"] is True
    assert report["missing_direct_observed_at_count"] == 1
    assert report["invalid_direct_observed_at_count"] == 1
    assert report["reversed_direct_observed_at_count"] == 1
    assert report["gap_count"] >= 3


def test_parent_observed_and_legacy_are_reported_but_never_satisfy_direct_completeness() -> None:
    observed_documents = _source_documents(
        chunk_temporal=("", ""),
        parent=("2026-07-01T01:00:00Z", "2026-07-01T02:00:00Z"),
    )
    legacy_documents = _source_documents(
        chunk_temporal=("", ""),
        legacy=("2026-07-01T01:00:00Z", "2026-07-01T02:00:00Z"),
    )

    observed_code, observed = _run_cli(_FakeCouchSource(observed_documents), _argv())
    legacy_code, legacy = _run_cli(_FakeCouchSource(legacy_documents), _argv())

    assert observed_code == legacy_code == 1
    assert observed["parent_observed_fallback_count"] == 2
    assert observed["direct_observed_at_valid_count"] == 0
    assert legacy["parent_legacy_fallback_count"] == 2
    assert legacy["direct_observed_at_valid_count"] == 0


def test_hybrid_field_families_and_naive_timestamps_are_malformed_not_fallbacks() -> None:
    documents = _source_documents(
        chunk_temporal=("", ""),
        parent=("2026-07-01T01:00:00Z", ""),
        legacy=("2026-07-01T01:00:00", "2026-07-01T02:00:00"),
    )
    documents[1]["observed_at_start"] = "2026-07-01T01:00:00Z"
    documents[1]["observed_at_end"] = ""

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["malformed_temporal_evidence_count"] == 2
    assert report["parent_observed_fallback_count"] == 0
    assert report["parent_legacy_fallback_count"] == 0


def test_index_preflight_is_explicit_and_fail_closed_before_any_scan() -> None:
    store = _FakeCouchSource(_source_documents(), indexed=False)

    code, report = _run_cli(store, _argv())

    assert code == 2
    assert report["error"] == "source_index_preflight_unindexed"
    assert report["gap_count"] > 0
    assert store.find_calls == []
    assert store.explain_calls[0]["index_name"] == DEFAULT_INDEX_NAME
    assert store.explain_calls[0]["index_design_document"] == DEFAULT_INDEX_DESIGN_DOCUMENT
    assert store.explain_calls[0]["allow_fallback"] is False


def test_partial_mango_index_blocks_before_any_scan() -> None:
    store = _FakeCouchSource(_source_documents(), partial_filter_selector={"project": "other"})

    code, report = _run_cli(store, _argv())

    assert code == 2
    assert report["error"] == "source_index_preflight_partial"
    assert store.find_calls == []


def test_limit_plus_one_overflow_blocks_without_claiming_complete_scan() -> None:
    documents = _source_documents()
    original = documents[1]
    documents.extend(
        [
            {**original, "_id": f"private-conversation-extra-{number}"}
            for number in range(3)
        ]
    )
    store = _FakeCouchSource(documents)

    code, report = _run_cli(store, _argv(limit=3))

    assert code == 1
    assert report["error"] == "scope_limit_exceeded"
    assert report["scan_exhausted"] is False
    assert report["gap_count"] > 0
    conversation_call = next(call for call in store.find_calls if call["doc_type"] == "conversation_chunk")
    assert conversation_call["limit"] == 4
    assert conversation_call["allow_fallback"] is False


def test_limit_is_per_family_and_global_bound_is_four_families() -> None:
    code, report = _run_cli(_FakeCouchSource(_source_documents()), _argv(limit=1))

    assert code == 0
    assert report["per_family_limit"] == 1
    assert report["global_document_limit"] == 4


def test_inventory_request_timeout_reserves_the_bounded_run_budget() -> None:
    assert _per_request_timeout_seconds(100) == 100 / 16
    assert _per_request_timeout_seconds(600) == 30


def test_coverage_manifest_is_a_cross_check_not_direct_temporal_evidence() -> None:
    documents = _source_documents(chunk_temporal=("", ""))
    documents[0]["observed_at_start"] = "2026-07-01T01:00:00Z"
    documents[0]["observed_at_end"] = "2026-07-01T02:00:00Z"

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["direct_observed_at_valid_count"] == 0
    assert report["parent_observed_fallback_count"] == 2
    assert report["coverage_manifest_temporal_evidence_count"] == 0
    assert report["gap_count"] > 0


def test_malformed_parent_or_manifest_observed_pairs_block_even_with_direct_child_evidence() -> None:
    cases = (
        (
            SourceDocType.TRANSCRIPT_SESSION,
            "malformed_transcript_session_observed_at_count",
        ),
        (
            SourceDocType.COVERAGE_MANIFEST,
            "malformed_coverage_manifest_observed_at_count",
        ),
    )
    malformed_pairs = (
        ("missing", "2026-07-01T01:00:00Z", ""),
        ("invalid", "not-a-time", "2026-07-01T02:00:00Z"),
        ("reversed", "2026-07-01T02:00:00Z", "2026-07-01T01:00:00Z"),
    )

    for doc_type, counter in cases:
        for _kind, start, end in malformed_pairs:
            documents = _source_documents()
            target = next(document for document in documents if document["doc_type"] == doc_type)
            target["observed_at_start"] = start
            target["observed_at_end"] = end

            code, report = _run_cli(_FakeCouchSource(documents), _argv())

            assert code == 1
            assert report["direct_observed_at_valid_count"] == 2
            assert report[counter] == 1
            assert report["malformed_temporal_evidence_count"] == 1
            assert report["temporal_complete"] is False
            assert report["gap_count"] > 0


def test_malformed_parent_observed_pair_is_counted_once_when_multiple_children_need_fallback() -> None:
    documents = _source_documents(
        chunk_temporal=("", ""),
        parent=("2026-07-01T01:00:00Z", ""),
    )

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["malformed_transcript_session_observed_at_count"] == 1
    assert report["malformed_temporal_evidence_count"] == 1
    assert report["malformed_missing_pair_count"] == 1
    assert report["gap_count"] > 0


def test_coverage_cross_check_blocks_manifest_only_orphan_missing_duplicate_and_session_mismatch() -> None:
    cases: list[tuple[str, list[dict], str]] = []

    manifest_only = _source_documents()
    manifest_only = [doc for doc in manifest_only if doc["doc_type"] != SourceDocType.CONVERSATION_CHUNK and doc["doc_type"] != SourceDocType.TOOL_EVIDENCE_BUNDLE]
    manifest_only[-1]["conversation_chunk_count"] = 0
    manifest_only[-1]["tool_evidence_bundle_count"] = 0
    cases.append(("manifest_only", manifest_only, "manifest_only_session_count"))

    orphan = _source_documents()
    orphan[1]["session_id_hash"] = "sha256:orphan-child-marker"
    cases.append(("orphan", orphan, "orphan_child_session_count"))

    missing = [doc for doc in _source_documents() if doc["doc_type"] != SourceDocType.COVERAGE_MANIFEST]
    cases.append(("missing", missing, "missing_manifest_session_count"))

    duplicate = _source_documents()
    duplicate.append({**duplicate[-1], "_id": "private-duplicate-manifest-marker"})
    cases.append(("duplicate", duplicate, "duplicate_manifest_count"))

    mismatch = _source_documents()
    mismatch[-1]["session_id_hash"] = "sha256:other-manifest-session-marker"
    cases.append(("mismatch", mismatch, "session_mismatch_count"))

    for _name, documents, counter in cases:
        code, report = _run_cli(_FakeCouchSource(documents), _argv())
        assert code == 1
        assert report[counter] > 0
        assert report["gap_count"] > 0


def test_coverage_count_mismatch_blocks_despite_direct_temporal_evidence() -> None:
    documents = _source_documents()
    manifest = next(doc for doc in documents if doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST)
    manifest["conversation_chunk_count"] = 2

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["coverage_count_mismatch_count"] == 1
    assert report["direct_observed_at_valid_count"] == 2
    assert report["gap_count"] > 0


def test_manifest_hashes_must_match_current_direct_children() -> None:
    cases: list[tuple[str, list[dict], str]] = []

    stale_conversation = _source_documents()
    next(doc for doc in stale_conversation if doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST)[
        "conversation_coverage_hash"
    ] = sha256_hash("stale-conversation-coverage")
    cases.append(("stale_conversation", stale_conversation, "conversation_coverage_hash_mismatch_count"))

    child_changed = _source_documents()
    next(doc for doc in child_changed if doc["doc_type"] == SourceDocType.CONVERSATION_CHUNK)[
        "content_hash"
    ] = sha256_hash("new-direct-child-content")
    cases.append(("child_changed", child_changed, "conversation_coverage_hash_mismatch_count"))

    stale_source = _source_documents()
    next(doc for doc in stale_source if doc["doc_type"] == SourceDocType.COVERAGE_MANIFEST)[
        "source_hash"
    ] = sha256_hash("stale-source")
    cases.append(("stale_source", stale_source, "source_hash_mismatch_count"))

    for _name, documents, counter in cases:
        code, report = _run_cli(_FakeCouchSource(documents), _argv())
        assert code == 1
        assert report[counter] > 0
        assert report["gap_count"] > 0


def test_missing_canonical_identity_or_hash_inputs_block_without_rendering_values() -> None:
    cases: list[tuple[str, SourceDocType, str, str]] = [
        ("chunk_id", SourceDocType.CONVERSATION_CHUNK, "_id", "invalid_conversation_chunk_identity_count"),
        ("chunk_revision", SourceDocType.CONVERSATION_CHUNK, "_rev", "invalid_conversation_chunk_identity_count"),
        ("bundle_id", SourceDocType.TOOL_EVIDENCE_BUNDLE, "_id", "invalid_tool_evidence_bundle_identity_count"),
        ("manifest_revision", SourceDocType.COVERAGE_MANIFEST, "_rev", "invalid_coverage_manifest_identity_count"),
        ("chunk_content", SourceDocType.CONVERSATION_CHUNK, "content_hash", "invalid_conversation_chunk_content_hash_count"),
        ("bundle_content", SourceDocType.TOOL_EVIDENCE_BUNDLE, "content_hash", "invalid_tool_evidence_bundle_content_hash_count"),
        ("bundle_coverage", SourceDocType.TOOL_EVIDENCE_BUNDLE, "coverage_hash", "invalid_tool_evidence_coverage_hash_count"),
        ("manifest_conversation_coverage", SourceDocType.COVERAGE_MANIFEST, "conversation_coverage_hash", "invalid_manifest_conversation_coverage_hash_count"),
        ("manifest_tool_coverage", SourceDocType.COVERAGE_MANIFEST, "tool_evidence_coverage_hash", "invalid_manifest_tool_evidence_coverage_hash_count"),
        ("manifest_source", SourceDocType.COVERAGE_MANIFEST, "source_hash", "invalid_manifest_source_hash_count"),
    ]

    for _name, doc_type, field, counter in cases:
        documents = _source_documents()
        next(doc for doc in documents if doc["doc_type"] == doc_type)[field] = ""

        code, report = _run_cli(_FakeCouchSource(documents), _argv())

        assert code == 1
        assert report[counter] == 1
        assert report["temporal_complete"] is False
        assert report["gap_count"] > 0


def test_malformed_session_id_hash_in_any_source_family_blocks_completion() -> None:
    cases = (
        (SourceDocType.TRANSCRIPT_SESSION, "invalid_transcript_session_session_id_hash_count"),
        (SourceDocType.CONVERSATION_CHUNK, "invalid_conversation_chunk_session_id_hash_count"),
        (SourceDocType.TOOL_EVIDENCE_BUNDLE, "invalid_tool_evidence_bundle_session_id_hash_count"),
        (SourceDocType.COVERAGE_MANIFEST, "invalid_coverage_manifest_session_id_hash_count"),
    )

    for doc_type, counter in cases:
        documents = _source_documents()
        next(doc for doc in documents if doc["doc_type"] == doc_type)["session_id_hash"] = "not-a-hash"

        code, report = _run_cli(_FakeCouchSource(documents), _argv())

        assert code == 1
        assert report[counter] == 1
        assert report["temporal_complete"] is False
        assert report["gap_count"] > 0


def test_duplicate_transcript_session_blocks_without_arbitrary_parent_choice() -> None:
    documents = _source_documents(chunk_temporal=("", ""))
    documents.append({**documents[0], "_id": "private-duplicate-session-marker"})

    code, report = _run_cli(_FakeCouchSource(documents), _argv())

    assert code == 1
    assert report["duplicate_transcript_session_count"] == 1
    assert report["parent_observed_fallback_count"] == 0
    assert report["gap_count"] > 0


def test_excessive_index_scan_blocks_and_preserves_safe_report() -> None:
    store = _FakeCouchSource(
        _source_documents(),
        execution_stats={"total_docs_examined": 23, "total_keys_examined": 23},
    )

    code, report = _run_cli(store, _argv(limit=10))

    assert code == 1
    assert report["error"] == "source_index_scan_bound_exceeded"
    assert report["gap_count"] > 0
    assert DEFAULT_INDEX_NAME not in json.dumps(report, sort_keys=True)


def test_keys_examined_over_per_family_bound_blocks_even_when_docs_are_low() -> None:
    store = _FakeCouchSource(
        _source_documents(),
        execution_stats={"total_docs_examined": 1, "total_keys_examined": 23},
    )

    code, report = _run_cli(store, _argv(limit=10))

    assert code == 1
    assert report["error"] == "source_index_scan_bound_exceeded"


def test_invalid_execution_stats_fail_closed_with_a_nonzero_gap() -> None:
    store = _FakeCouchSource(
        _source_documents(),
        execution_stats={"total_docs_examined": "many", "total_keys_examined": 1},
    )

    code, report = _run_cli(store, _argv())

    assert code == 2
    assert report["error"] == "source_execution_stats_invalid"
    assert report["gap_count"] > 0


def test_sequence_drift_and_runtime_bound_are_non_successes() -> None:
    drift_code, drift = _run_cli(_FakeCouchSource(_source_documents(), sequences=["before", "after"]), _argv())
    ticks = iter((0.0, 0.0, 2.0))

    try:
        inventory_temporal_evidence(
            source_store=_FakeCouchSource(_source_documents()),
            project="neurons",
            limit=10,
            max_runtime_seconds=1,
            require_complete_scan=True,
            monotonic=lambda: next(ticks),
        )
    except RuntimeError as exc:
        timeout_error = str(exc)
    else:  # pragma: no cover - contract failure message below is more useful
        timeout_error = ""

    assert drift_code == 1
    assert drift["source_changed_during_scan"] is True
    assert drift["scan_exhausted"] is False
    assert drift["gap_count"] > 0
    assert drift["source_update_seq_start_hash"] != drift["source_update_seq_end_hash"]
    assert timeout_error == "runtime_bound_exceeded"


def test_inventory_digest_is_deterministic_over_redacted_revision_manifest() -> None:
    documents = _source_documents()
    first = inventory_temporal_evidence(
        source_store=_FakeCouchSource(documents),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )
    second = inventory_temporal_evidence(
        source_store=_FakeCouchSource(list(reversed(documents))),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert first["inventory_digest"] == second["inventory_digest"]


def test_inventory_source_has_no_state_db_or_shadow_ingest_log_reference() -> None:
    source = Path(__file__).parents[1] / "lib/agent_knowledge/couchdb_source/temporal_evidence_inventory.py"

    rendered = source.read_text(encoding="utf-8")
    assert "state_db" not in rendered
    assert "shadow_ingest_log" not in rendered


def test_http_store_sends_explicit_index_and_disables_mango_fallback_without_writes() -> None:
    calls: list[tuple[str, str, dict]] = []

    def transport(method: str, url: str, headers: dict, body: bytes) -> ProxyResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        calls.append((method, url, payload))
        if url.endswith("/_explain"):
            return ProxyResponse(
                200,
                json.dumps(
                    {
                        "index": {
                            "type": "json",
                            "name": DEFAULT_INDEX_NAME,
                            "ddoc": DEFAULT_INDEX_DESIGN_DOCUMENT,
                            "def": {"fields": [{"project": "asc"}, {"doc_type": "asc"}]},
                        }
                    }
                ).encode(),
            )
        if url.endswith("/private-db"):
            return ProxyResponse(200, b'{"update_seq":"opaque-sequence"}')
        return ProxyResponse(
            200,
            b'{"docs":[],"execution_stats":{"total_docs_examined":0,"total_keys_examined":0}}',
        )

    store = CouchDBHttpSourceStore(base_url="https://private-couch-marker.invalid", db="private-db", transport=transport)
    explanation = store.explain_find(
        selector={"project": "neurons", "doc_type": "conversation_chunk"},
        fields=["_id", "_rev", "observed_at_start", "observed_at_end"],
        limit=11,
        index_name=DEFAULT_INDEX_NAME,
        index_design_document=DEFAULT_INDEX_DESIGN_DOCUMENT,
        allow_fallback=False,
    )
    sequence = store.read_change_sequence()
    found = store.find_by_type_with_execution_stats(
        "conversation_chunk",
        selector={"project": "neurons", "doc_type": "coverage_manifest"},
        fields=["_id", "_rev", "observed_at_start", "observed_at_end"],
        limit=11,
        use_index=[DEFAULT_INDEX_DESIGN_DOCUMENT, DEFAULT_INDEX_NAME],
        allow_fallback=False,
    )

    assert explanation["index"]["name"] == DEFAULT_INDEX_NAME
    assert sequence == "opaque-sequence"
    assert found == {
        "documents": [],
        "execution_stats": {"total_docs_examined": 0, "total_keys_examined": 0},
    }
    assert [method for method, _url, _payload in calls] == ["POST", "GET", "POST"]
    assert calls[1][1].endswith("/private-db")
    assert "_changes" not in calls[1][1]
    assert calls[0][2]["use_index"] == [DEFAULT_INDEX_DESIGN_DOCUMENT, DEFAULT_INDEX_NAME]
    assert calls[0][2]["allow_fallback"] is False
    assert calls[2][2]["use_index"] == [DEFAULT_INDEX_DESIGN_DOCUMENT, DEFAULT_INDEX_NAME]
    assert calls[2][2]["allow_fallback"] is False
    assert calls[2][2]["execution_stats"] is True
    assert calls[2][2]["selector"]["doc_type"] == "conversation_chunk"


def test_snapshot_copies_are_not_counted_as_family_documents() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]

    snapshot_copy = {
        **{k: v for k, v in documents[0].items() if k not in {"_id", "_rev"}},
        "_id": f"transcript_session:snapshot:{session_hash[-24:]}",
        "_rev": "2-snapshot-revision-marker",
        "source_snapshot_schema_version": "couchdb_source_revision_snapshot.v1",
        "source_snapshot_scope": sha256_hash("snapshot-scope"),
        "source_snapshot_origin_id": documents[0]["_id"],
        "source_hash": sha256_hash("session-source:snapshot"),
    }
    chunk_snapshot_copy = {
        **{k: v for k, v in documents[1].items() if k not in {"_id", "_rev"}},
        "_id": f"conversation_chunk:snapshot:{session_hash[-24:]}",
        "_rev": "4-chunk-snapshot-revision-marker",
        "source_snapshot_schema_version": "couchdb_source_revision_snapshot.v1",
        "source_snapshot_scope": sha256_hash("snapshot-scope"),
        "source_snapshot_origin_id": documents[1]["_id"],
    }
    bundle_snapshot_copy = {
        **{k: v for k, v in documents[2].items() if k not in {"_id", "_rev"}},
        "_id": f"tool_evidence_bundle:snapshot:{session_hash[-24:]}",
        "_rev": "5-bundle-snapshot-revision-marker",
        "source_snapshot_schema_version": "couchdb_source_revision_snapshot.v1",
        "source_snapshot_scope": sha256_hash("snapshot-scope"),
        "source_snapshot_origin_id": documents[2]["_id"],
    }
    _refresh_manifest_hashes([snapshot_copy, chunk_snapshot_copy, bundle_snapshot_copy, documents[3]])
    control_documents = _active_controls(
        session_hash,
        [snapshot_copy, chunk_snapshot_copy, bundle_snapshot_copy],
        documents[3]["source_hash"],
    )
    documents[3]["active_source_manifest_id"] = control_documents[1]["_id"]
    with_dupes = [*documents, snapshot_copy, chunk_snapshot_copy, bundle_snapshot_copy, *control_documents]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource(with_dupes),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["duplicate_transcript_session_count"] == 0
    assert report["gap_count"] == 0
    assert report["temporal_complete"] is True


def _marked_copy(document: dict, *, scope: str, suffix: str) -> dict:
    copied = {key: value for key, value in document.items() if key not in {"_id", "_rev"}}
    copied.update(
        {
            "_id": f"{document['doc_type']}:snapshot:{suffix}",
            "_rev": f"2-{suffix}",
            "source_snapshot_schema_version": "couchdb_source_revision_snapshot.v1",
            "source_snapshot_scope": scope,
            "source_snapshot_origin_id": document["_id"],
        }
    )
    if document["doc_type"] == SourceDocType.TRANSCRIPT_SESSION:
        copied["source_hash"] = sha256_hash(f"session-source:{suffix}")
    return copied


def _active_controls(session_hash: str, source_documents: list[dict], source_hash: str) -> list[dict]:
    descriptors = [
        _source_member_descriptor(source, session_id_hash=session_hash)
        for source in source_documents
    ]
    revision = _source_revision(descriptors)
    manifest_id = source_revision_manifest_doc_id(session_hash, revision)
    memberships = []
    member_documents = []
    for source, descriptor in zip(source_documents, descriptors, strict=True):
        member_id = source_revision_member_doc_id(session_hash, revision, source["_id"])
        membership = {
            "member_id": member_id,
            **descriptor,
            "member_hash": _member_hash(
                session_id_hash=session_hash,
                source_revision=revision,
                descriptor=descriptor,
                member_id=member_id,
            ),
        }
        memberships.append(membership)
        member_documents.append(
            {
                "_id": member_id,
                "doc_type": SourceDocType.SOURCE_REVISION_MEMBER,
                "project": "neurons",
                "session_id_hash": session_hash,
                "source_revision": revision,
                **membership,
            }
        )
    manifest_hash = _manifest_hash(
        session_id_hash=session_hash,
        source_revision=revision,
        source_hash=source_hash,
        members=memberships,
        provenance={},
    )
    return [
        {
            "_id": active_source_revision_pointer_doc_id(session_hash),
            "doc_type": SourceDocType.ACTIVE_SOURCE_REVISION,
            "project": "neurons",
            "session_id_hash": session_hash,
            "active_revision": revision,
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "source_hash": source_hash,
        },
        {
            "_id": manifest_id,
            "doc_type": SourceDocType.SOURCE_REVISION_MANIFEST,
            "project": "neurons",
            "session_id_hash": session_hash,
            "source_revision": revision,
            "manifest_hash": manifest_hash,
            "source_hash": source_hash,
            "provenance": {},
            "members": memberships,
        },
        *member_documents,
    ]


def test_partial_active_snapshot_is_blocked() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    scope = sha256_hash("partial-scope")
    snapshot_session = _marked_copy(documents[0], scope=scope, suffix="partial-session")
    snapshot_chunk = _marked_copy(documents[1], scope=scope, suffix="partial-chunk")
    source_hash = documents[3]["source_hash"]
    missing_bundle = _marked_copy(documents[2], scope=scope, suffix="missing-bundle")
    controls = _active_controls(
        session_hash,
        [snapshot_session, snapshot_chunk, missing_bundle],
        source_hash,
    )

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, snapshot_session, snapshot_chunk, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_marked_document_without_session_id_is_not_dropped() -> None:
    documents = _source_documents()
    marked = _marked_copy(
        documents[1],
        scope=sha256_hash("empty-session-scope"),
        suffix="empty-session-chunk",
    )
    marked["session_id_hash"] = ""

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, marked]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["missing_chunk_session_id_count"] > 0
    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_active_pointer_selects_one_generation_when_old_scope_coexists() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active_scope = sha256_hash("active-scope")
    old_scope = sha256_hash("old-scope")
    active = [
        _marked_copy(documents[index], scope=active_scope, suffix=f"active-{index}")
        for index in range(3)
    ]
    old = [
        _marked_copy(documents[index], scope=old_scope, suffix=f"old-{index}")
        for index in range(3)
    ]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(
        session_hash,
        active,
        documents[3]["source_hash"],
    )
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *old, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["duplicate_transcript_session_count"] == 0
    assert report["gap_count"] == 0
    assert report["temporal_complete"] is True


def test_current_copy_wrapped_in_generic_snapshot_is_valid() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    scope = sha256_hash("nested-snapshot-scope")
    active = [
        _marked_copy(documents[index], scope=scope, suffix=f"nested-{index}")
        for index in range(3)
    ]
    for row in active:
        row["current_source_scope"] = sha256_hash(f"current:{row['_id']}")
        row["supersedes_source_document_hash"] = sha256_hash(str(row["source_snapshot_origin_id"]))
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] == 0
    assert report["temporal_complete"] is True


def test_missing_revision_member_is_blocked() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active = [
        _marked_copy(documents[index], scope=sha256_hash("missing-member-scope"), suffix=f"missing-{index}")
        for index in range(3)
    ]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls[:-1]]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_invalid_pointer_identity_is_blocked() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active = [
        _marked_copy(documents[index], scope=sha256_hash("invalid-pointer-scope"), suffix=f"pointer-{index}")
        for index in range(3)
    ]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]
    controls[0]["_id"] = "active_source_revision:wrong"

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_marked_coverage_manifest_is_not_silently_accepted() -> None:
    documents = _source_documents()
    documents[3].update(
        {
            "source_snapshot_schema_version": "couchdb_source_revision_snapshot.v1",
            "source_snapshot_scope": sha256_hash("coverage-marker-scope"),
            "source_snapshot_origin_id": documents[3]["_id"],
        }
    )

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource(documents),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_mutually_rehashed_pointer_and_manifest_is_blocked_by_source_hash() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active = [
        _marked_copy(documents[index], scope=sha256_hash("forged-source-scope"), suffix=f"forged-{index}")
        for index in range(3)
    ]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, sha256_hash("forged-source-hash"))
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_document_hash_generation_is_not_accepted_without_body_proof() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active_session = _marked_copy(
        documents[0],
        scope=sha256_hash("document-hash-scope"),
        suffix="document-hash-session",
    )
    active_session.pop("source_hash", None)
    active_session["body"] = "tampered body"
    active = [active_session, _marked_copy(documents[1], scope=sha256_hash("document-hash-scope"), suffix="document-hash-chunk"), _marked_copy(documents[2], scope=sha256_hash("document-hash-scope"), suffix="document-hash-bundle")]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_snapshot_origin_binding_is_verified() -> None:
    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    scope = sha256_hash("origin-binding-scope")
    active = [
        _marked_copy(documents[index], scope=scope, suffix=f"origin-{index}")
        for index in range(3)
    ]
    active[1]["source_snapshot_origin_id"] = "conversation_chunk:missing-origin"
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    documents[3]["active_source_manifest_id"] = controls[1]["_id"]

    report = inventory_temporal_evidence(
        source_store=_FakeCouchSource([*documents, *active, *controls]),
        project="neurons",
        limit=10,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False


def test_oversized_session_membership_is_blocked() -> None:
    """A manifest with more members than the per-session ceiling is malformed, not a query oracle."""

    documents = _source_documents()
    session_hash = documents[0]["session_id_hash"]
    active = [
        _marked_copy(documents[index], scope=sha256_hash("bound-scope"), suffix=f"bound-{index}")
        for index in range(3)
    ]
    _refresh_manifest_hashes([*active, documents[3]])
    controls = _active_controls(session_hash, active, documents[3]["source_hash"])
    manifest = controls[1]
    descriptors = [
        _source_member_descriptor(source, session_id_hash=session_hash)
        for source in active
    ]
    revision = _source_revision(descriptors)
    # limit=2 -> per-session ceiling = 2*2+1 = 5; the fixture holds 3, so add
    # 3 forged memberships to reach 6 > 5.
    memberships = list(manifest["members"])
    for forged_index in range(3):
        forged_source_id = f"conversation_chunk:forged-overflow-{forged_index}"
        forged_member_id = source_revision_member_doc_id(session_hash, revision, forged_source_id)
        forged_descriptor = {**descriptors[1], "source_document_id": forged_source_id}
        memberships.append(
            {
                "member_id": forged_member_id,
                **forged_descriptor,
                "member_hash": _member_hash(
                    session_id_hash=session_hash,
                    source_revision=revision,
                    descriptor=forged_descriptor,
                    member_id=forged_member_id,
                ),
            }
        )
    manifest_hash = _manifest_hash(
        session_id_hash=session_hash,
        source_revision=revision,
        source_hash=documents[3]["source_hash"],
        members=memberships,
        provenance={},
    )
    manifest["members"] = memberships
    manifest["manifest_hash"] = manifest_hash
    pointer = controls[0]
    pointer["manifest_hash"] = manifest_hash

    store = _FakeCouchSource(documents + active + controls)
    # Materialize the forged members so only the bound check, not a missing
    # member document, can be the reason for blocking.
    forged_member_docs = [
        {
            "_id": m["member_id"],
            "doc_type": SourceDocType.SOURCE_REVISION_MEMBER,
            "project": "neurons",
            "session_id_hash": session_hash,
            "source_revision": revision,
            **m,
        }
        for m in memberships[3:]
    ]
    store.documents = [*store.documents, *forged_member_docs]

    report = inventory_temporal_evidence(
        source_store=store,
        project="neurons",
        limit=2,
        max_runtime_seconds=10,
        require_complete_scan=True,
    )

    assert report["gap_count"] > 0
    assert report["temporal_complete"] is False
