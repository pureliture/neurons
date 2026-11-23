from __future__ import annotations

from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from test_pending_state_sweep import (
    PROJECT,
    PROVIDER,
    _PgStore,
    _ready_lookup,
    _seed,
    _write_checkpoint,
)


def _ids(sessions: list[dict]) -> set[str]:
    return {str(session["session_id_hash"]) for session in sessions}


def _select(store, pg, checkpoint: Path, *, shard_index: int, shard_count: int) -> set[str]:
    return _ids(
        build_cli._select_sessions_for_cycle(
            store,
            limit=0,
            project=PROJECT,
            provider=PROVIDER,
            backend="postgres_pgvector",
            mode="full",
            dead_letter_lookup=None,
            persist=False,
            ready_content_lookup=_ready_lookup(pg),
            shard_index=shard_index,
            shard_count=shard_count,
        )
    )


def test_shards_partition_and_cover_all_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(tmp_path / "checkpoint.json"))
    store = InMemoryCouchDBSourceStore()
    all_ids = {_seed(store, f"shard-{index}") for index in range(12)}
    pg = _PgStore(ready_ids=set())

    baseline = _select(store, pg, tmp_path / "checkpoint.json", shard_index=0, shard_count=1)
    shard_0 = _select(store, pg, tmp_path / "checkpoint.json", shard_index=0, shard_count=2)
    shard_1 = _select(store, pg, tmp_path / "checkpoint.json", shard_index=1, shard_count=2)

    assert baseline == all_ids
    assert shard_0 & shard_1 == set()
    assert shard_0 | shard_1 == baseline
    assert shard_0 <= all_ids and shard_1 <= all_ids


def test_pending_state_sweep_is_sharded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(tmp_path / "checkpoint.json"))
    store = InMemoryCouchDBSourceStore()
    all_ids = {_seed(store, f"pending-{index}") for index in range(10)}
    pg = _PgStore(ready_ids=set())
    _write_checkpoint(tmp_path / "checkpoint.json")

    shard_0 = _select(store, pg, tmp_path / "checkpoint.json", shard_index=0, shard_count=2)
    shard_1 = _select(store, pg, tmp_path / "checkpoint.json", shard_index=1, shard_count=2)

    assert shard_0 & shard_1 == set()
    assert shard_0 | shard_1 == all_ids


def test_checkpoint_pending_ids_are_filtered_for_final_list(tmp_path, monkeypatch) -> None:
    base = tmp_path / "checkpoint.json"
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(base))
    store = InMemoryCouchDBSourceStore()
    all_ids = {_seed(store, f"checkpoint-{index}") for index in range(8)}
    pg = _PgStore(ready_ids=set())
    build_cli._write_selection_checkpoint(
        str(base),
        "100",
        full_walk_in_progress=True,
        embedding_profile="",
        representation="session_v1",
        pending_session_ids=sorted(all_ids),
    )

    selected = _select(store, pg, base, shard_index=0, shard_count=2)

    assert selected
    assert all(build_cli._session_in_shard(sid, 0, 2) for sid in selected)
    assert selected <= all_ids


def test_shard_config_defaults_env_and_validation() -> None:
    assert build_cli._shard_config(None, None, {}) == (0, 1)
    assert build_cli._shard_config(None, None, {
        "SESSION_MEMORY_BUILD_SHARD_INDEX": "2",
        "SESSION_MEMORY_BUILD_SHARD_COUNT": "3",
    }) == (2, 3)
    assert build_cli._shard_config("1", "4", {}) == (1, 4)
    for index, count in (("2", "2"), ("-1", "2"), ("0", "0"), ("bad", "2")):
        with pytest.raises(ValueError):
            build_cli._shard_config(index, count, {})
