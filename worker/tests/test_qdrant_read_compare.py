"""M7: read-compare / recall-parity harness (pure compute, fakes only)."""

from __future__ import annotations

from agent_knowledge.rag_ingress.qdrant_read_compare import (
    READ_COMPARE_SCHEMA,
    compare_recall,
    recall_parity_passes,
)


def _hits(*content_hashes):
    return [{"content_hash": ch, "summary": "s"} for ch in content_hashes]


def test_full_parity_when_mirror_covers_primary():
    fixtures = {
        "q1": (_hits("a", "b"), _hits("a", "b", "c")),  # mirror superset -> exact
        "q2": (_hits("d"), _hits("d")),
    }
    report = compare_recall(
        ["q1", "q2"],
        primary_fetch=lambda q: fixtures[q][0],
        mirror_fetch=lambda q: fixtures[q][1],
        k=10,
    )
    assert report["schema_version"] == READ_COMPARE_SCHEMA
    assert report["total_count"] == 2
    assert report["matched_count"] == 2
    assert report["mismatch_count"] == 0
    assert report["mean_recall_at_k"] == 1.0
    assert report["raw_query_printed"] is False


def test_mismatch_and_partial_recall_detected():
    fixtures = {
        "q1": (_hits("a", "b"), _hits("a")),  # mirror missing 'b' -> not exact, recall 0.5
    }
    report = compare_recall(
        ["q1"], primary_fetch=lambda q: fixtures[q][0], mirror_fetch=lambda q: fixtures[q][1], k=10
    )
    assert report["matched_count"] == 0
    assert report["mismatch_count"] == 1
    assert report["mean_recall_at_k"] == 0.5
    # query text is hashed, not echoed
    assert report["per_query"][0]["query_hash"].startswith("sha256:")
    assert "q1" not in str(report)


def test_top_k_truncation_limits_comparison_window():
    primary = _hits("a", "b", "c")
    mirror = _hits("a", "b", "z")
    # at k=2 only {a,b} considered on each side -> exact subset, recall 1.0
    report = compare_recall(["q"], primary_fetch=lambda q: primary, mirror_fetch=lambda q: mirror, k=2)
    assert report["matched_count"] == 1
    assert report["mean_recall_at_k"] == 1.0


def test_recall_parity_passes_gate_helper():
    ok = {"mismatch_count": 0, "mean_recall_at_k": 0.96}
    assert recall_parity_passes(ok, min_mean_recall_at_k=0.95) is True
    # exact required by default -> any mismatch fails
    assert recall_parity_passes({"mismatch_count": 1, "mean_recall_at_k": 0.99}, min_mean_recall_at_k=0.95) is False
    # below recall threshold fails even with exact match
    assert recall_parity_passes({"mismatch_count": 0, "mean_recall_at_k": 0.80}, min_mean_recall_at_k=0.95) is False
    # exact not required -> recall threshold alone governs
    assert recall_parity_passes({"mismatch_count": 3, "mean_recall_at_k": 0.97}, min_mean_recall_at_k=0.95, require_exact=False) is True


def test_empty_cohort_is_vacuously_full_recall():
    report = compare_recall([], primary_fetch=lambda q: [], mirror_fetch=lambda q: [], k=10)
    assert report["total_count"] == 0
    assert report["mismatch_count"] == 0
    assert report["mean_recall_at_k"] == 1.0
