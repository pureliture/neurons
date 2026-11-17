"""Live PostgreSQL coverage for the substring identifier lane."""

from __future__ import annotations

import pytest

from agent_knowledge.model_connectors import LOCAL_EMBEDDING_DIM
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore, make_dummy_vector
from test_local_embedding_m2_pg import _card, _local

PROJECT = "lexlane"


def _seed(store: PgVectorStore) -> None:
    store.upsert_card(_card("lex-gocd", "GoCD 릴리스 게이트", project=PROJECT, title="GoCD release gate"))
    store.upsert_card(_card("lex-m1", "M1 릴리스", project=PROJECT, title="M1 release"))
    store.upsert_card(_card("lex-m10", "M10 릴리스", project=PROJECT, title="M10 release"))
    store.upsert_card(_card("lex-kr", "마이그레이션 계획", project=PROJECT, title="마이그레이션 계획"))
    store.upsert_card(_card("lex-other", "GoCD 릴리스 게이트", project="other-project", title="GoCD release gate"))
    # The lane shares the vector lane's eligibility contract: only cards with a
    # ready local embedding are retrievable.
    for job in store.claim_local_outbox_leases("lex-seed", batch_size=20, lease_seconds=30):
        assert store.complete_local_outbox_cas(
            outbox_id=job.outbox_id,
            worker_id="lex-seed",
            fencing_token=job.fencing_token,
            vector=make_dummy_vector(42, LOCAL_EMBEDDING_DIM),
        )


def test_lexical_lane_matches_hyphenated_and_case_insensitive_identifiers(isolated_pg_store) -> None:
    store = _local(isolated_pg_store)
    _seed(store)

    hits = store.search_cards_lexical(project=PROJECT, text="go-cd", limit=10)

    assert [card["memory_id"] for card in hits] == ["lex-gocd"]
    assert hits[0]["project"] == PROJECT


def test_lexical_lane_matches_korean_substring(isolated_pg_store) -> None:
    store = _local(isolated_pg_store)
    _seed(store)

    hits = store.search_cards_lexical(project=PROJECT, text="마이그레", limit=10)

    assert [card["memory_id"] for card in hits] == ["lex-kr"]


def test_lexical_lane_short_identifier_boundary_does_not_match_longer_identifier(isolated_pg_store) -> None:
    store = _local(isolated_pg_store)
    _seed(store)

    hits = store.search_cards_lexical(project=PROJECT, text="M1", limit=10)

    ids = [card["memory_id"] for card in hits]
    assert "lex-m1" in ids
    assert "lex-m10" not in ids


def test_lexical_lane_keeps_project_scope(isolated_pg_store) -> None:
    store = _local(isolated_pg_store)
    _seed(store)

    assert store.search_cards_lexical(project="missing-project", text="GoCD", limit=10) == []
    hits = store.search_cards_lexical(project=PROJECT, text="GoCD", limit=10)
    assert {card["memory_id"] for card in hits} == {"lex-gocd"}


def test_lexical_lane_requires_probe_tokens_and_positive_limit(isolated_pg_store) -> None:
    store = _local(isolated_pg_store)
    _seed(store)

    assert store.search_cards_lexical(project=PROJECT, text="a", limit=10) == []
    with pytest.raises(ValueError):
        store.search_cards_lexical(project=PROJECT, text="GoCD", limit=0)


def test_lexical_lane_has_no_alias_expansion(isolated_pg_store) -> None:
    """Alias mapping is out of scope: ``m2v`` must not invent ``model2vec`` hits."""

    store = _local(isolated_pg_store)
    _seed(store)
    store.upsert_card(_card("lex-model2vec", "model2vec 임베딩", project=PROJECT, title="model2vec 임베딩"))

    assert store.search_cards_lexical(project=PROJECT, text="m2v", limit=10) == []
