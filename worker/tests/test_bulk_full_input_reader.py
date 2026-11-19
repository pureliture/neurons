"""TDD: reader using real PG authority cards and real resolver join."""

from __future__ import annotations

import os
import uuid

import pytest

from agent_knowledge.llm_brain_core.bulk_full_input import build_session_grounding_fact
from agent_knowledge.llm_brain_core.bulk_full_input_reader import (
    FullInputReader,
    PgAuthorityCardStore,
)

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no local PG")
PREFIX = uuid.uuid4().hex[:8] + "-"


def _reader():
    dsn = os.environ["TRIAL_PG_DSN"]
    store = PgAuthorityCardStore(dsn)
    return FullInputReader(dsn), store


def test_authorized_card_accepted_and_hash_mismatch_rejected():
    reader, store = _reader()
    mid = PREFIX + "card"
    store.upsert_card(memory_id=mid, project=PREFIX, content_hash="sha256:" + "a" * 64,
                      provenance="legacy")
    fact = build_session_grounding_fact(project=PREFIX, memory_id=mid,
                                        content_hash="sha256:" + "a" * 64,
                                        shadow_group="shadow_x", chunk_key=PREFIX + "k")
    result = reader.resolve(PREFIX, (fact,), eligible_shadow_groups=("shadow_x",))
    assert result["status"] == "verified"
    assert len(result["cards"]) == 1

    bad = build_session_grounding_fact(project=PREFIX, memory_id=mid,
                                       content_hash="sha256:" + "b" * 64,
                                       shadow_group="shadow_x", chunk_key=PREFIX + "k2")
    result2 = reader.resolve(PREFIX, (bad,), eligible_shadow_groups=("shadow_x",))
    assert result2["cards"] == []
    assert result2["status"] == "mismatch"


def test_shadow_fact_excluded_until_eligible():
    reader, store = _reader()
    mid = PREFIX + "shadow-card"
    store.upsert_card(memory_id=mid, project=PREFIX, content_hash="sha256:" + "c" * 64,
                      provenance="shadow")
    fact = build_session_grounding_fact(project=PREFIX, memory_id=mid,
                                        content_hash="sha256:" + "c" * 64,
                                        shadow_group="shadow_eligible",
                                        chunk_key=PREFIX + "k")
    assert reader.resolve(PREFIX, (fact,), eligible_shadow_groups=())["cards"] == []
    result = reader.resolve(PREFIX, (fact,),
                            eligible_shadow_groups=("shadow_eligible",))
    assert len(result["cards"]) == 1
    assert result["shadow_cards"] == 1


def test_legacy_and_shadow_same_memory_id_merged_once():
    reader, store = _reader()
    mid = PREFIX + "merged"
    store.upsert_card(memory_id=mid, project=PREFIX, content_hash="sha256:" + "d" * 64,
                      provenance="legacy")
    legacy = build_session_grounding_fact(project=PREFIX, memory_id=mid,
                                          content_hash="sha256:" + "d" * 64,
                                          shadow_group="", chunk_key=PREFIX + "l")
    legacy.payload["legacy_eligible"] = True
    shadow = build_session_grounding_fact(project=PREFIX, memory_id=mid,
                                          content_hash="sha256:" + "d" * 64,
                                          shadow_group="shadow_merge",
                                          chunk_key=PREFIX + "s")
    result = reader.resolve(PREFIX, (legacy, shadow),
                            eligible_shadow_groups=("shadow_merge",))
    assert len(result["cards"]) == 1
    assert result["legacy_cards"] >= 1 and result["shadow_cards"] >= 1
