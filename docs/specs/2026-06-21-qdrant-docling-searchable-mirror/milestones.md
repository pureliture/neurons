# Milestones - qdrant-docling-searchable-mirror

## M0 Contract Audit And SoT Docs
- status: done
- evidence: requirements/design/milestones created from audit of `RagTargetAdapter`, `rag_ingress`, `session_memory`, and `ontology` contracts.

## M1 Adapter Protocol And Fake-Client Tests
- status: done
- evidence: `uv run pytest -q tests/test_qdrant_docling_mirror.py` passed; `QdrantDoclingMirrorAdapter` covers submit/status/natural-key/search over fake Qdrant.

## M2 Docling Normalization And Privacy Fail-Closed
- status: done
- evidence: `test_qdrant_docling_adapter_upserts_normalized_markdown_and_reports_status` stores normalized markdown; text and nested metadata privacy tests block before upsert.

## M3 Qdrant Natural-Key, Status, Search
- status: done
- evidence: natural-key, status, filtered mirror candidate search, collection probe, and vector-size guard tests passed.

## M4 Digest-Bound Dual-Write/Read-Compare Gate Report
- status: done
- evidence: evidence packet is required for comparison-ready; compare mismatch blocks; boolean claims alone keep failover blocked.

## M5 Apple Silicon Local-First Smoke Shape
- status: done
- evidence: adapter optional imports are lazy; fake-client unit smoke is network-free; `searchable-mirror` extra is optional; design cites Qdrant local mode `QdrantClient(":memory:")` / `path=...`.

## M6 Ubuntu Host Production-Gate Shape
- status: done
- evidence: `test_searchable_mirror_gate_cli_is_dry_run_redacted_and_no_go` exercises `rag-ingress-state searchable-mirror-gate --dry-run --redact-paths --evidence-packet ...` with no network/mutation.

## M7 RetiredIndexBridge Failover Remains Blocked
- status: done
- evidence: gate report keeps `production_authority_status=NO-GO`; valid evidence can make only `comparison_gate_status=ready_for_operator_cutover_packet`; `index_failover_status` remains blocked.
