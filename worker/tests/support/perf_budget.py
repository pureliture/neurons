"""Explicit opt-in lane for wall-clock performance budgets.

Latency budgets measured against in-memory test doubles depend on host load
(shared CI agents vs. GitHub runners), so they are not a correctness signal.
They are enforced only when ``LBRAIN_ENFORCE_PERF_BUDGET=1``. Functional,
recall, and gate-consistency assertions always run.
"""
from __future__ import annotations

import os

PERF_BUDGET_ENV = "LBRAIN_ENFORCE_PERF_BUDGET"


def perf_budget_enforced() -> bool:
    return os.environ.get(PERF_BUDGET_ENV, "") == "1"
