"""M0 seams: write seam (worker -> projection_state_store) and read seam
(projection_cli resume union)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.graph import (
    FakeGraphMemoryAdapter,
    NullGraphMemoryAdapter,
)
from agent_knowledge.llm_brain_core.ledger_adapter import (
    LedgerGraphProjectionStateStore,
)
from agent_knowledge.llm_brain_core.projection import GraphProjectionWorker


def _h(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _card(memory_id, card_type, summary, typed_payload, *, project="neurons"):
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": card_type,
        "scope": "project",
        "project": project,
        "provider": "codex",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "confidence": 0.9,
        "source_refs": [{"source_ref_id": "src_seam", "content_hash": _h("source")}],
        "derived_from": ["evt_seam"],
        "typed_payload": typed_payload,
    }


def test_write_seam_records_projected_and_duplicate_only(tmp_path: Path):
    # (d) write seam: projected + duplicate are recorded; skipped_disabled and
    # failures are NOT (plane separation).
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph, projection_state_store=store)
    card = _card("mem_seam", "task", "Seam task", {"task_state": "Seam task"})

    first = worker.project_memory_cards([card], project="neurons").to_dict()
    projected_ids = set(first["episode_ids"])
    assert first["projected"] == 1
    assert store.list_projected_ids(project="neurons") == projected_ids

    # Re-projecting the same card returns duplicate, still recorded (idempotent).
    second = worker.project_memory_cards([card], project="neurons").to_dict()
    assert second["duplicates"] == 1
    assert store.list_projected_ids(project="neurons") == projected_ids


def test_write_seam_does_not_record_skipped_disabled(tmp_path: Path):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    worker = GraphProjectionWorker(
        NullGraphMemoryAdapter(), projection_state_store=store
    )
    card = _card("mem_disabled", "task", "Disabled task", {"task_state": "Disabled task"})

    report = worker.project_memory_cards([card], project="neurons").to_dict()

    assert report["skipped_disabled"] == 1
    # Graph disabled is a no-op plane; nothing is durably recorded as projected.
    assert store.list_projected_ids(project="neurons") == set()


def test_write_seam_store_none_preserves_behavior(tmp_path: Path):
    # store=None means the worker behaves exactly as before (behavior-preserving).
    graph = FakeGraphMemoryAdapter()
    worker = GraphProjectionWorker(graph)
    card = _card("mem_none", "task", "None task", {"task_state": "None task"})

    report = worker.project_memory_cards([card], project="neurons").to_dict()

    assert report["projected"] == 1
    assert report["status"] == "succeeded"


def test_read_seam_resume_union_skips_durably_projected(tmp_path: Path):
    # (e) read seam idempotency: a second run over the same window does zero
    # upsert round-trips because the store-backed resume ids are unioned into the
    # resume set.
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))

    class _Counting:
        def __init__(self):
            self.calls = 0

        def upsert_episode(self, episode):
            self.calls += 1
            return "inserted"

        def search_context(self, *, brain_id, query, entity_types=None, limit=10):
            from agent_knowledge.llm_brain_core.models import GraphMemoryResult

            return GraphMemoryResult(status="available")

    counting = _Counting()
    worker = GraphProjectionWorker(counting, projection_state_store=store)
    card = _card("mem_read_seam", "task", "Read seam task", {"task_state": "Read seam task"})

    first = worker.project_memory_cards([card], project="neurons").to_dict()
    assert first["projected"] == 1
    assert counting.calls == 1

    resume_ids = store.list_projected_ids(project="neurons")
    counting.calls = 0
    second = worker.project_memory_cards(
        [card], project="neurons", resume_projected_ids=resume_ids
    ).to_dict()

    assert second["skipped_resumed"] == 1
    assert second["projected"] == 0
    assert counting.calls == 0


def _accepted_task_card(memory_id: str, project: str = "neurons") -> dict:
    summary = f"Durable resume fixture {memory_id}"
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": "task",
        "scope": "project",
        "project": project,
        "provider": "claude",
        "title": summary,
        "summary": summary,
        "render_text": summary,
        "lifecycle_state": "accepted",
        "judgment_state": "none",
        "status": "accepted",
        "approval_state": "approved",
        "governance_tier": "medium",
        "freshness": "current",
        "currentness": "current",
        "confidence": 0.9,
        "confidence_basis": "durable resume fixture",
        "source_refs": [{"source_ref_id": "src_durable", "content_hash": _h("source")}],
        "evidence_refs": [],
        "evidence_hashes": [_h(memory_id)],
        "derived_from": [],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": "",
        "updated_at": "2026-06-19T00:00:00Z",
        "typed_payload": {
            "task_state": summary,
            "next_action": "Project this card",
            "blocker": "",
            "owner_hint": "neurons",
            "status": "open",
        },
    }


def test_cli_durable_resume_skips_without_resume_file(tmp_path: Path, monkeypatch, capsys):
    # (e) read seam at CLI level: a second run with NO --resume-projected-ids file
    # still skips the already-projected episode because the durable store is
    # unioned into the resume set. This is the durability the resume file alone
    # could not give.
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path).upsert_llm_brain_memory_card(_accepted_task_card("mem_durable_cli"))
    graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: graph,
    )

    argv = [
        "brain-project",
        "--ledger",
        str(ledger_path),
        "--project",
        "neurons",
        "--skip-source-refs",
        "--enable-graph",
    ]

    assert neuron_main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["projection"]["projected"] == 1

    # Second run, same window, no resume file supplied.
    assert neuron_main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["projection"]["skipped_resumed"] == 1
    assert second["projection"]["projected"] == 0

    # The durable store records the projection across runs.
    store = LedgerGraphProjectionStateStore(Ledger(ledger_path))
    assert store.list_projected_ids(project="neurons") == set(
        first["projection"]["episode_ids"]
    )
