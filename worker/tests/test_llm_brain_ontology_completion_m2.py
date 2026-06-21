"""M2: extract_entities path idempotency, composite (episode_id, extraction_level)
tracking, level-aware resume, and write-time redaction hard gate.

All unit/stub: no live Neo4j, no live LLM. The Graphiti backend is a fake whose
add_episode records calls and returns a stub AddEpisodeResults-shaped object so
the write-time redaction gate can be exercised deterministically.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_knowledge.cli import main as neuron_main
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.graph import FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.graphiti_adapter import (
    GraphitiNeo4jGraphMemoryAdapter,
)
from agent_knowledge.llm_brain_core.ledger_adapter import (
    EXTRACTION_LEVEL_ENTITY,
    EXTRACTION_LEVEL_EPISODIC,
    LedgerGraphProjectionStateStore,
    _GRAPH_PROJECTION_STATE_SCHEMA,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from agent_knowledge.llm_brain_core.projection import GraphProjectionWorker


# --------------------------------------------------------------------------- #
# fixtures / fakes
# --------------------------------------------------------------------------- #


def _h(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _episode(
    natural_suffix: str,
    *,
    entity_type: str = "Task",
    project: str = "neurons",
    payload_extra: dict | None = None,
) -> OntologyEpisode:
    payload = {
        "task_state": f"State {natural_suffix}",
        "project": project,
        "brain_id": f"/project/{project}",
    }
    payload.update(payload_extra or {})
    return OntologyEpisode.from_payload(
        event_id=f"evt:{natural_suffix}",
        entity_type=entity_type,
        natural_id=f"task:{natural_suffix}",
        payload=payload,
        observed_at="2026-06-19T00:00:00+00:00",
        reference_time="2026-06-19T00:00:00+00:00",
    )


class _EntityFakeGraphiti:
    """Graphiti stand-in for the entity (add_episode) path.

    Records add_episode calls (including kwargs so the uuid pass-through can be
    asserted) and returns a stub results object holding the EntityNode/EntityEdge
    text the write-time redaction gate inspects.
    """

    def __init__(
        self,
        *,
        nodes: list | None = None,
        edges: list | None = None,
    ) -> None:
        self.added: list[dict] = []
        self._nodes = nodes or []
        self._edges = edges or []
        self.driver = SimpleNamespace()

    async def add_episode(self, **kwargs):
        self.added.append(dict(kwargs))
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=kwargs.get("uuid")),
            episodic_edges=[],
            nodes=list(self._nodes),
            edges=list(self._edges),
            communities=[],
            community_edges=[],
        )


def _card(memory_id, summary, typed_payload, *, project="neurons"):
    return {
        "memory_id": memory_id,
        "brain_id": f"/project/{project}",
        "card_type": "task",
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
        "source_refs": [{"source_ref_id": "src_m2", "content_hash": _h("source")}],
        "derived_from": ["evt_m2"],
        "typed_payload": typed_payload,
    }


# --------------------------------------------------------------------------- #
# TDD 1: adapter entity idempotency + uuid pass-through
# --------------------------------------------------------------------------- #


def test_entity_path_passes_uuid_and_is_idempotent_on_reupsert():
    graphiti = _EntityFakeGraphiti()
    extracted: set[str] = set()

    async def _entity_extracted(driver, episode_id):
        _ = driver
        return episode_id in extracted

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_entity_extracted,
    )
    episode = _episode("entity-idem")

    first = adapter.upsert_episode(episode)
    # Simulate the entity pass having extracted entities for this episode.
    extracted.add(episode.episode_id)
    second = adapter.upsert_episode(episode)

    assert first == "inserted"
    assert second == "duplicate"
    # add_episode ran exactly once; the duplicate short-circuits before a second
    # extraction (no LLM re-billing).
    assert len(graphiti.added) == 1
    # uuid=episode_id is passed so Graphiti reuses the existing EpisodicNode
    # instead of minting a random uuid.
    assert graphiti.added[0]["uuid"] == episode.episode_id


def test_entity_path_existing_episodic_node_still_runs_entity_extraction():
    # An EpisodicNode existing (episodic pass) is NOT enough to skip the entity
    # pass: the entity-extracted probe is distinct from episode-exists, so the
    # entity pass still runs when only the episodic node exists.
    graphiti = _EntityFakeGraphiti()

    async def _episode_exists(driver, episode_id):
        _ = (driver, episode_id)
        return True  # episodic node already present

    async def _entity_extracted(driver, episode_id):
        _ = (driver, episode_id)
        return False  # but no entities extracted yet

    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        episode_exists=_episode_exists,
        entity_extracted=_entity_extracted,
    )

    result = adapter.upsert_episode(_episode("entity-after-episodic"))

    assert result == "inserted"
    assert len(graphiti.added) == 1


# --------------------------------------------------------------------------- #
# TDD 5: write-time redaction hard gate
# --------------------------------------------------------------------------- #


def _never_extracted(driver, episode_id):
    async def _probe():
        _ = (driver, episode_id)
        return False

    return _probe()


def test_write_time_redaction_blocks_private_path_in_extracted_node_name():
    graphiti = _EntityFakeGraphiti(
        nodes=[SimpleNamespace(name="see /Users/example/private", summary="ok")],
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_never_extracted,
    )

    with pytest.raises(ValueError, match="private or raw content"):
        adapter.upsert_episode(_episode("redact-node-name"))


def test_write_time_redaction_blocks_secret_assignment_in_extracted_edge_fact():
    graphiti = _EntityFakeGraphiti(
        edges=[SimpleNamespace(name="RELATES_TO", fact="config sets API_KEY=abc123")],
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_never_extracted,
    )

    with pytest.raises(ValueError, match="private or raw content"):
        adapter.upsert_episode(_episode("redact-edge-fact"))


def test_write_time_redaction_does_not_echo_raw_secret_in_exception():
    graphiti = _EntityFakeGraphiti(
        nodes=[SimpleNamespace(name="x", summary="PASSWORD=hunter2-supersecret")],
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_never_extracted,
    )

    with pytest.raises(ValueError) as exc:
        adapter.upsert_episode(_episode("redact-no-echo"))
    # The raw secret value must never appear in the raised message.
    assert "hunter2-supersecret" not in str(exc.value)
    assert "EntityNode.summary" in str(exc.value)


def test_write_time_redaction_allows_public_safe_extraction():
    graphiti = _EntityFakeGraphiti(
        nodes=[SimpleNamespace(name="Graph adapter", summary="A derived index.")],
        edges=[SimpleNamespace(name="RELATES_TO", fact="adapter stores episodes.")],
    )
    adapter = GraphitiNeo4jGraphMemoryAdapter(
        graphiti,
        default_group_id="/project/neurons",
        extract_entities=True,
        entity_extracted=_never_extracted,
    )

    assert adapter.upsert_episode(_episode("redact-clean")) == "inserted"


# --------------------------------------------------------------------------- #
# TDD 3: store composite (episode_id, extraction_level) tracking
# --------------------------------------------------------------------------- #


def test_store_records_both_levels_for_same_episode(tmp_path: Path):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episode = _episode("two-pass")

    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_EPISODIC)
    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_ENTITY)

    # Both levels resolve the same episode_id; level filters narrow to one pass.
    assert store.list_projected_ids() == {episode.episode_id}
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_EPISODIC) == {
        episode.episode_id
    }
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_ENTITY) == {
        episode.episode_id
    }


def test_store_level_filter_excludes_unmatched_pass(tmp_path: Path):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episode = _episode("episodic-only")

    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_EPISODIC)

    # Recorded only episodic: an entity-level filter sees nothing.
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_ENTITY) == set()
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_EPISODIC) == {
        episode.episode_id
    }


def test_store_default_level_is_episodic(tmp_path: Path):
    # mark_projected without an explicit level records the episodic pass
    # (behavior-preserving for the M0 two-arg call shape).
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episode = _episode("default-level")

    store.mark_projected(episode, "inserted")

    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_EPISODIC) == {
        episode.episode_id
    }


def test_store_count_rows_per_level(tmp_path: Path):
    ledger = _ledger(tmp_path)
    store = LedgerGraphProjectionStateStore(ledger)
    episode = _episode("rowcount")

    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_EPISODIC)
    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_ENTITY)
    # Re-marking the same (episode_id, level) is idempotent (upsert, not a 3rd row).
    store.mark_projected(episode, "duplicate", EXTRACTION_LEVEL_ENTITY)

    with ledger._connect() as connection:
        rows = connection.execute(
            "SELECT episode_id, extraction_level FROM llm_brain_graph_projection_state "
            "WHERE episode_id = ? ORDER BY extraction_level",
            (episode.episode_id,),
        ).fetchall()
    levels = sorted(str(row["extraction_level"]) for row in rows)
    assert levels == [EXTRACTION_LEVEL_ENTITY, EXTRACTION_LEVEL_EPISODIC]


# --------------------------------------------------------------------------- #
# TDD 2: projection worker records at the adapter's extraction level
# --------------------------------------------------------------------------- #


class _EntityLevelAdapter:
    """Worker-facing adapter stub flagged as the entity pass."""

    _extract_entities = True

    def __init__(self):
        self.calls = 0

    def upsert_episode(self, episode):
        self.calls += 1
        return "inserted"

    def search_context(self, *, brain_id, query, entity_types=None, limit=10):
        from agent_knowledge.llm_brain_core.models import GraphMemoryResult

        return GraphMemoryResult(status="available")


def test_worker_records_entity_level_when_adapter_extracts_entities(tmp_path: Path):
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    worker = GraphProjectionWorker(_EntityLevelAdapter(), projection_state_store=store)
    card = _card("mem_entity_level", "Entity level task", {"task_state": "Entity level task"})

    report = worker.project_memory_cards([card], project="neurons").to_dict()
    projected_ids = set(report["episode_ids"])

    assert report["projected"] == 1
    # Recorded at the entity level, not the episodic default.
    assert store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_ENTITY
    ) == projected_ids
    assert (
        store.list_projected_ids(
            project="neurons", extraction_level=EXTRACTION_LEVEL_EPISODIC
        )
        == set()
    )


def test_worker_episodic_only_record_does_not_block_entity_pass(tmp_path: Path):
    # An id projected episodic-only must NOT be in the entity-level resume set, so
    # an entity pass over the same id still runs.
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episodic_adapter = FakeGraphMemoryAdapter()  # no _extract_entities -> episodic
    GraphProjectionWorker(
        episodic_adapter, projection_state_store=store
    ).project_memory_cards(
        [_card("mem_promote", "Promote task", {"task_state": "Promote task"})],
        project="neurons",
    )

    episodic_ids = store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_EPISODIC
    )
    entity_resume = store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_ENTITY
    )

    assert episodic_ids  # episodic pass recorded
    assert entity_resume == set()  # entity pass not blocked by the episodic record


# --------------------------------------------------------------------------- #
# TDD 4 + 6: CLI entity pass resume narrowing and force-reextract
# --------------------------------------------------------------------------- #


def _accepted_task_card(memory_id: str, *, project: str = "neurons") -> dict:
    summary = f"M2 CLI fixture {memory_id}"
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
        "confidence_basis": "m2 cli fixture",
        "source_refs": [{"source_ref_id": "src_m2_cli", "content_hash": _h("source")}],
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


class _EntityFlagFakeGraph(FakeGraphMemoryAdapter):
    """FakeGraphMemoryAdapter that reports as the entity pass to the worker/CLI."""

    _extract_entities = True


def test_cli_entity_pass_runs_when_only_episodic_recorded(tmp_path, monkeypatch, capsys):
    # TDD 4/6: an episodic-only record must not make the entity pass skip the
    # episode. The entity pass narrows resume to the entity level, finds nothing,
    # and projects.
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path).upsert_llm_brain_memory_card(_accepted_task_card("mem_cli_entity"))

    # First run: episodic-only (default adapter, no entity flag).
    episodic_graph = FakeGraphMemoryAdapter()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: episodic_graph,
    )
    base_argv = [
        "brain-project",
        "--ledger",
        str(ledger_path),
        "--project",
        "neurons",
        "--skip-source-refs",
        "--enable-graph",
    ]
    assert neuron_main(base_argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["projection"]["projected"] == 1

    # Second run: entity pass over the SAME window. It must project (entity-level
    # resume is empty), not skip-resume on the episodic record.
    entity_graph = _EntityFlagFakeGraph()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: entity_graph,
    )
    assert neuron_main([*base_argv, "--extract-entities"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["projection"]["projected"] == 1
    assert second["projection"]["skipped_resumed"] == 0

    # Durable store now has both levels for the episode.
    store = LedgerGraphProjectionStateStore(Ledger(ledger_path))
    assert store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_EPISODIC
    )
    assert store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_ENTITY
    )


def test_cli_entity_pass_resumes_on_entity_level_record(tmp_path, monkeypatch, capsys):
    # TDD 4: a second entity pass over a window already projected AT the entity
    # level skip-resumes (no re-projection).
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path).upsert_llm_brain_memory_card(_accepted_task_card("mem_cli_entity_resume"))
    entity_graph = _EntityFlagFakeGraph()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: entity_graph,
    )
    argv = [
        "brain-project",
        "--ledger",
        str(ledger_path),
        "--project",
        "neurons",
        "--skip-source-refs",
        "--enable-graph",
        "--extract-entities",
    ]

    assert neuron_main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["projection"]["projected"] == 1

    assert neuron_main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["projection"]["skipped_resumed"] == 1
    assert second["projection"]["projected"] == 0


def test_cli_reextract_entities_bypasses_entity_level_resume(tmp_path, monkeypatch, capsys):
    # TDD 6: --reextract-entities re-runs the entity pass even when an entity-level
    # record already exists (force promotion / re-extraction).
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path).upsert_llm_brain_memory_card(_accepted_task_card("mem_cli_reextract"))
    entity_graph = _EntityFlagFakeGraph()
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        lambda **kwargs: entity_graph,
    )
    base = [
        "brain-project",
        "--ledger",
        str(ledger_path),
        "--project",
        "neurons",
        "--skip-source-refs",
        "--enable-graph",
        "--extract-entities",
    ]

    assert neuron_main(base) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["projection"]["projected"] == 1

    # Without --reextract-entities this would skip-resume; with it the entity pass
    # re-runs (the FakeGraph reports a duplicate, still a non-skip round-trip).
    assert neuron_main([*base, "--reextract-entities"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["projection"]["skipped_resumed"] == 0
    # The entity pass hit the adapter (duplicate), not a resume skip.
    assert second["projection"]["duplicates"] == 1


# --------------------------------------------------------------------------- #
# TDD 7: behavior-preserving regression (extract_entities=False default)
# --------------------------------------------------------------------------- #


def test_default_episodic_path_unchanged_by_m2(tmp_path: Path):
    # The default (no entity flag) worker still records the episodic level and a
    # default-args list_projected_ids returns it (M0 resume shape preserved).
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    worker = GraphProjectionWorker(FakeGraphMemoryAdapter(), projection_state_store=store)
    card = _card("mem_default", "Default task", {"task_state": "Default task"})

    report = worker.project_memory_cards([card], project="neurons").to_dict()

    assert report["projected"] == 1
    projected = set(report["episode_ids"])
    # Default list (no level arg) and explicit episodic level both return it.
    assert store.list_projected_ids(project="neurons") == projected
    assert store.list_projected_ids(
        project="neurons", extraction_level=EXTRACTION_LEVEL_EPISODIC
    ) == projected


# --------------------------------------------------------------------------- #
# migration / backfill seam
# --------------------------------------------------------------------------- #


_PRE_M2_SCHEMA = """
CREATE TABLE llm_brain_graph_projection_state (
    episode_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT '',
    natural_id TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    brain_id TEXT DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    ontology_version TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    upsert_result TEXT NOT NULL DEFAULT '',
    projected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def test_pre_m2_table_is_migrated_and_existing_rows_backfilled(tmp_path: Path):
    # A ledger whose projection_state table predates M2 (sole episode_id PK, no
    # extraction_level) is lazily migrated: the column is added, existing rows
    # backfill to 'episodic', and the composite-unique shape replaces the sole PK
    # so both passes can coexist afterwards.
    ledger = _ledger(tmp_path)
    with ledger._connect() as connection:
        connection.execute("DROP TABLE IF EXISTS llm_brain_graph_projection_state")
        connection.executescript(_PRE_M2_SCHEMA)
        connection.execute(
            """
            INSERT INTO llm_brain_graph_projection_state (
                episode_id, project, entity_type, natural_id, group_id, brain_id,
                content_hash, ontology_version, extractor_version, upsert_result,
                projected_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "episode:legacyrow000000",
                "neurons",
                "Task",
                "task:legacy",
                "brain_neurons",
                "/project/neurons",
                _h("legacy"),
                "1.0.0",
                "0.1.0",
                "inserted",
                "2026-06-19T00:00:00Z",
                "2026-06-19T00:00:00Z",
            ),
        )

    # Constructing the store triggers _ensure_schema -> _migrate_extraction_level.
    store = LedgerGraphProjectionStateStore(ledger)

    # Existing legacy row survives and is backfilled to the episodic level.
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_EPISODIC) == {
        "episode:legacyrow000000"
    }
    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT extraction_level FROM llm_brain_graph_projection_state "
            "WHERE episode_id = ?",
            ("episode:legacyrow000000",),
        ).fetchone()
        assert str(row["extraction_level"]) == EXTRACTION_LEVEL_EPISODIC
        # Sole episode_id PK is gone (composite-unique shape now).
        pk_cols = [
            str(info["name"])
            for info in connection.execute(
                "PRAGMA table_info(llm_brain_graph_projection_state)"
            ).fetchall()
            if info["pk"]
        ]
        assert pk_cols != ["episode_id"]

    # After migration the legacy episode can also be recorded at the entity level
    # (two rows for one episode_id, which the pre-M2 sole PK forbade).
    episode = _episode("legacy")  # same natural_id family; new episode object
    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_ENTITY)
    assert store.list_projected_ids(extraction_level=EXTRACTION_LEVEL_ENTITY) == {
        episode.episode_id
    }


def test_fresh_table_migration_is_noop(tmp_path: Path):
    # On a freshly-created (already new-shape) table the migration is a no-op: the
    # column exists, there is no sole PK to rebuild, and rows are untouched.
    store = LedgerGraphProjectionStateStore(_ledger(tmp_path))
    episode = _episode("fresh")
    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_EPISODIC)
    store.mark_projected(episode, "inserted", EXTRACTION_LEVEL_ENTITY)

    # Re-construct the store (re-runs _ensure_schema/migration) and confirm rows
    # are preserved, not duplicated or dropped.
    store2 = LedgerGraphProjectionStateStore(store._ledger)
    assert store2.list_projected_ids() == {episode.episode_id}
    assert store2.list_projected_ids(extraction_level=EXTRACTION_LEVEL_ENTITY) == {
        episode.episode_id
    }


def test_cli_plain_run_preserves_env_extract_entities_default(tmp_path, monkeypatch, capsys):
    # Behavior-preserving: with NO entity flag, run_projection passes
    # extract_entities=None, so the env override is not applied and the env-driven
    # LLM_BRAIN_GRAPH_EXTRACT_ENTITIES default reaches the adapter untouched.
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path).upsert_llm_brain_memory_card(_accepted_task_card("mem_env_default"))

    captured_environ = {}

    def _capture(environ=None, **kwargs):
        captured_environ["value"] = environ
        return _EntityFlagFakeGraph()

    monkeypatch.setenv("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES", "true")
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.projection_cli.build_graph_adapter_from_env",
        _capture,
    )

    rc = neuron_main(
        [
            "brain-project",
            "--ledger",
            str(ledger_path),
            "--project",
            "neurons",
            "--skip-source-refs",
            "--enable-graph",
        ]
    )

    assert rc == 0
    # No flag -> no environ override object; the builder falls back to os.environ
    # (where the test set the entity flag true), preserving the env default.
    assert captured_environ["value"] is None


def test_schema_constant_declares_composite_uniqueness():
    # Guard: the single-source schema constant must enforce composite uniqueness,
    # not a sole episode_id PRIMARY KEY (so both passes coexist).
    assert "UNIQUE(episode_id, extraction_level)" in _GRAPH_PROJECTION_STATE_SCHEMA
    assert "episode_id TEXT PRIMARY KEY" not in _GRAPH_PROJECTION_STATE_SCHEMA
