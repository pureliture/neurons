"""Readiness contract for the LLM-brain eval lane.

This module names what the eval lane can and cannot prove. It keeps the
dev-only answer-key/demo harness separate from product readiness and runtime
verification claims.
"""

from __future__ import annotations

from typing import Any

EVAL_READINESS_SCHEMA_VERSION = "llm_brain_eval_readiness.v1"

EVAL_READINESS_BLOCKER_CODES = (
    "live_mining_provider",
    "scheduler_install",
    "machine_origin",
    "candidates_persistence",
    "runtime_tripwire",
    "golden_content_completion",
)

_VERIFICATION_LEVELS: tuple[dict[str, Any], ...] = (
    {
        "code": "api_shape_only",
        "description": "API health/status shape check only",
        "requires": ["api_health", "api_status_shape"],
        "runtime_verified": False,
    },
    {
        "code": "api_queue_smoke",
        "description": "API plus queue smoke evidence",
        "requires": ["api_health", "jetstream_stream", "worker_pressure_gate"],
        "runtime_verified": False,
    },
    {
        "code": "local_unit_contract",
        "description": "local unit or contract test evidence",
        "requires": ["focused_tests"],
        "runtime_verified": False,
    },
    {
        "code": "read_only_runtime",
        "description": "read-only runtime observation without mutation",
        "requires": ["configured_runtime_read", "redacted_evidence"],
        "runtime_verified": False,
    },
    {
        "code": "full_e2e_business",
        "description": "end-to-end business verification across persisted and recallable memory",
        "requires": [
            "persistence",
            "mirror_or_index",
            "graph_or_projection",
            "recall_read_path",
        ],
        "runtime_verified": False,
    },
)

VERIFICATION_LEVEL_CODES: tuple[str, ...] = tuple(
    level["code"] for level in _VERIFICATION_LEVELS
)


def build_eval_readiness_report() -> dict[str, Any]:
    """Return the public-safe readiness claim for the current eval lane."""

    return {
        "schema_version": EVAL_READINESS_SCHEMA_VERSION,
        "lane": "worker_eval",
        "classification": "dev_only_harness",
        "product_readiness_gate": False,
        "runtime_verified": False,
        "support_claim": "authored",
        "approval_required_before_live_mutation": True,
        "open_blocker_codes": list(EVAL_READINESS_BLOCKER_CODES),
        "verification_levels": [dict(level) for level in _VERIFICATION_LEVELS],
    }
