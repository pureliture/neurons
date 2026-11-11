"""Live e2-tiny concurrency verification test for Neurons PostgreSQL compat functions.

Execution Rules & Scope:
1. DESIGNATED ENTRY ONLY: Placed outside standard tests/ to prevent accidental collection in general CI.
   Must be invoked explicitly via manifest argv.
2. MANDATORY MODE: If ATLAS_TEST_PG_DSN is absent, test FAILS (BLOCKS) with exit code 1,
   ensuring the suite cannot silently pass without live target verification when dispatched.
3. RELATIVE IMPORTS ONLY: Resolves adapter and helpers strictly relative to this script's directory.
   Zero absolute user paths, zero external unpinned imports. Verifies import origins.
4. SMOKE TEST SCOPE: Concurrency test is strictly a concurrent startup contention smoke test.
   Actual live catalog collision is unverified; live recovery path is NOT claimed as verified.
5. Strict 3-connection slot budget, isolated UUID database, zero pg_terminate_backend.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest

# Determine worker directory strictly relative to this file
SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name in ("scripts", "candidate_e2_neurons") else SCRIPT_DIR
WORKER_LIB = WORKER_DIR / "lib"

# Insert strictly relative paths
if str(WORKER_LIB) not in sys.path and WORKER_LIB.is_dir():
    sys.path.insert(0, str(WORKER_LIB))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from neurons_e2_postgres_helpers import (
    DatabaseContext,
    isolated_neurons_test_database,
    run_mixed_concurrency_race,
)

try:
    from agent_knowledge.postgres_db_adapter import PostgresLedgerDbAdapter
except ImportError:
    PostgresLedgerDbAdapter = None


def verify_committed_import_origins() -> dict[str, str]:
    """Verifies that adapter and helpers originate from the committed worktree, not external unpinned paths."""
    import agent_knowledge.postgres_db_adapter as adapter_mod
    import neurons_e2_postgres_helpers as helpers_mod

    adapter_path = Path(adapter_mod.__file__).resolve()
    helpers_path = Path(helpers_mod.__file__).resolve()

    origins = {
        "adapter": str(adapter_path),
        "helpers": str(helpers_path),
    }
    # Print non-sensitive origin paths for verifiable evidence
    print(f"VERIFIED_IMPORT_ORIGIN_ADAPTER: {adapter_path}")
    print(f"VERIFIED_IMPORT_ORIGIN_HELPERS: {helpers_path}")

    # Strictly enforce that origins reside within the committed worktree and NOT in unpinned external paths
    assert "r41-compat-concurrency-isolate" in str(adapter_path), f"Adapter path mismatch: {adapter_path}"
    assert "r41-compat-concurrency-isolate" in str(helpers_path), f"Helpers path mismatch: {helpers_path}"
    assert "candidate_e2_neurons" not in str(adapter_path), "External unpinned adapter prohibited"
    assert "candidate_e2_neurons" not in str(helpers_path), "External unpinned helpers prohibited"
    return origins


def test_live_e2_mixed_concurrency_race():
    """Validates concurrent startup contention smoke on e2-tiny."""
    origins = verify_committed_import_origins()
    assert origins["adapter"] is not None
    assert origins["helpers"] is not None

    dsn = os.environ.get("ATLAS_TEST_PG_DSN")
    if not dsn:
        pytest.fail("ATLAS_TEST_PG_DSN is required; execution BLOCKED (Mandatory Mode Enforcement)")

    if PostgresLedgerDbAdapter is None:
        pytest.fail("Failed to import PostgresLedgerDbAdapter from committed worktree")

    # Allocate exactly ONE UUID database, execute mixed concurrent start smoke, and verify cleanup
    with isolated_neurons_test_database(project_prefix="bridge", connect_timeout=10) as ctx:
        results = run_mixed_concurrency_race(
            ctx,
            new_adapter_factory=PostgresLedgerDbAdapter,
            timeout_seconds=10.0,
        )
        assert results["new"] == "new_ok"
