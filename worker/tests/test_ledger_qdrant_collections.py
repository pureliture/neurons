"""M3: ledger qdrant_collections registry (additive, no network).

Parallel to ragflow_datasets. Authority for logical_name -> Qdrant collection
mapping, vector params, and enable state. Does not touch any live Qdrant.
"""

from __future__ import annotations

from agent_knowledge.ledger import Ledger


def _ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


def test_upsert_and_get_qdrant_collection(tmp_path):
    ledger = _ledger(tmp_path)
    row = ledger.upsert_qdrant_collection(
        logical_name="derived-memory-items",
        collection="neurons_mirror_bge_m3_v1",
        embedding_model="bge-m3",
        vector_size=1024,
        distance="Cosine",
        payload_index_version="v1",
    )
    assert row["logical_name"] == "derived-memory-items"
    assert row["collection"] == "neurons_mirror_bge_m3_v1"
    assert row["vector_size"] == 1024
    assert row["enabled"] == 1

    fetched = ledger.get_qdrant_collection("derived-memory-items")
    assert fetched is not None
    assert fetched["embedding_model"] == "bge-m3"
    assert ledger.get_qdrant_collection("missing") is None


def test_upsert_qdrant_collection_is_idempotent_update(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.upsert_qdrant_collection(
        logical_name="derived-memory-items", collection="c1", vector_size=384
    )
    row = ledger.upsert_qdrant_collection(
        logical_name="derived-memory-items", collection="c2", vector_size=1024, embedding_model="bge-m3"
    )
    assert row["collection"] == "c2"
    assert row["vector_size"] == 1024
    assert row["embedding_model"] == "bge-m3"
    assert len(ledger.list_qdrant_collections()) == 1


def test_list_qdrant_collections_sorted(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.upsert_qdrant_collection(logical_name="b-role", collection="cb")
    ledger.upsert_qdrant_collection(logical_name="a-role", collection="ca")
    names = [row["logical_name"] for row in ledger.list_qdrant_collections()]
    assert names == ["a-role", "b-role"]


def test_qdrant_collection_enabled_is_fail_closed_for_unknown(tmp_path):
    ledger = _ledger(tmp_path)
    # unknown collection -> NOT enabled (fail-closed)
    assert ledger._qdrant_collection_is_enabled("never-registered") is False
    assert ledger._qdrant_collection_is_enabled("") is False

    ledger.upsert_qdrant_collection(logical_name="derived-memory-items", collection="reg1")
    assert ledger._qdrant_collection_is_enabled("reg1") is True


def test_qdrant_collections_table_survives_reopen(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    Ledger(path).upsert_qdrant_collection(logical_name="r", collection="c", vector_size=1024)
    # re-open: schema is idempotent, row persists
    reopened = Ledger(path)
    assert reopened.get_qdrant_collection("r")["collection"] == "c"
