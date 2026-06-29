"""Read-only RetiredIndexBridge transcript-memory source-of-truth mapping."""

from agent_knowledge.index_client import transcript_memory_records_from_retired_index_bridge
from agent_knowledge.session_memory.memory_regeneration import (
    RetiredIndexBridgeTranscriptMemorySource,
    _chunk_record_from_retired_index_bridge,
)


SESSION = "sha256:abcdef012345aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FRAGMENT = "abcdef012345"


def _meta(**overrides):
    meta = {
        "knowledge_id": "kn_1",
        "chunk_id": "chunk_1",
        "result_type": "conversation_chunk",
        "type": "conversation_chunk",
        "provider": "codex",
        "project": "workspace-index-advisor",
        "session_id_hash": SESSION,
        "turn_start_index": "1",
        "turn_end_index": "3",
        "part_index": "1",
        "part_count": "1",
        "char_start": "0",
        "char_end": "11",
        "content_hash": "sha256:abc",
        "redaction_version": "redaction.v2",
        "source_status": "source_locator_private_spool_only",
        "domain": "agent_memory",
    }
    meta.update(overrides)
    return meta


def _doc(document_id, *, frag=FRAGMENT, **meta_overrides):
    return {
        "id": document_id,
        "name": f"ak-conv-codex-workspace-index-advisor-{frag}-t0001-0003-20260613T000000Z-{document_id}.md",
        "meta_fields": _meta(**meta_overrides),
    }


class _FakeRetiredIndexBridge:
    def __init__(self, docs, contents):
        self._docs = list(docs)
        self._contents = dict(contents)
        self.retrieve_calls = []
        self.list_calls = []

    def list_documents(self, dataset_id, *, keywords="", page_size=100):
        self.list_calls.append((dataset_id, keywords, page_size))
        if keywords:
            return [doc for doc in self._docs if keywords in str(doc.get("name") or "")]
        return list(self._docs)

    def list_document_chunks(self, dataset_id, document_id, *, page_size=100, max_pages=100):
        return list(self._contents.get(document_id, []))

    def retrieve(self, query, dataset_ids, *, filters=None, limit=10, document_ids=None, **kwargs):
        self.retrieve_calls.append({"document_ids": list(document_ids or []), "limit": limit})
        hits = []
        for document_id in document_ids or []:
            for content in self._contents.get(document_id, []):
                hits.append({"document_id": document_id, "content": content})
        return hits

    def get_document_meta(self, dataset_id, document_id):
        for doc in self._docs:
            if str(doc.get("id")) == document_id:
                return doc
        return None

    def list_transcript_memory_chunks(self, **kwargs):
        return transcript_memory_records_from_retired_index_bridge(self, ["ds_1"], **kwargs)


def test_records_map_to_chunk_records_without_ledger():
    rag = _FakeRetiredIndexBridge(
        docs=[_doc("doc_1")],
        contents={"doc_1": ["hello world"]},
    )
    records = transcript_memory_records_from_retired_index_bridge(
        rag, ["ds_1"], project="workspace-index-advisor", provider="codex", session_id_hash=SESSION
    )

    assert len(records) == 1
    rec = _chunk_record_from_retired_index_bridge(records[0])
    assert rec.session_id_hash == SESSION
    assert rec.redacted_text == "hello world"
    assert rec.knowledge_id == "kn_1"
    assert rec.part_count == 1
    assert rag.list_calls[0][1] == FRAGMENT


def test_design_source_works_through_adapter_and_is_ledger_free():
    rag = _FakeRetiredIndexBridge(docs=[_doc("doc_1")], contents={"doc_1": ["x"]})
    source = RetiredIndexBridgeTranscriptMemorySource(rag)

    recs = source.list_conversation_chunks(
        session_id_hash=SESSION,
        provider="codex",
        project="workspace-index-advisor",
    )

    assert len(recs) == 1
    assert recs[0].redacted_text == "x"


def test_mismatched_session_meta_is_filtered_out():
    rag = _FakeRetiredIndexBridge(
        docs=[_doc("doc_1", session_id_hash="sha256:other")],
        contents={"doc_1": ["x"]},
    )

    records = transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION)

    assert records == []


def test_subchunks_of_same_document_are_joined_in_hit_order():
    rag = _FakeRetiredIndexBridge(
        docs=[_doc("doc_1")],
        contents={"doc_1": ["part-a ", "part-b"]},
    )

    records = transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION)

    assert len(records) == 1
    assert records[0]["content"] == "part-a part-b"


def test_multiple_documents_for_session_all_returned():
    rag = _FakeRetiredIndexBridge(
        docs=[_doc("doc_1", chunk_id="c1"), _doc("doc_2", chunk_id="c2", turn_start_index="4", turn_end_index="6")],
        contents={"doc_1": ["a"], "doc_2": ["b"]},
    )

    records = transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION)

    assert sorted(r["content"] for r in records) == ["a", "b"]


def test_overlapping_window_granularities_reduced_to_clean_cover():
    rag = _FakeRetiredIndexBridge(
        docs=[
            _doc("doc_wide", chunk_id="w", turn_start_index="73", turn_end_index="85"),
            _doc("doc_narrow", chunk_id="n", turn_start_index="73", turn_end_index="73", content_hash="sha256:n"),
            _doc("doc_next", chunk_id="x", turn_start_index="86", turn_end_index="92", content_hash="sha256:x"),
        ],
        contents={"doc_wide": ["wide"], "doc_narrow": ["narrow"], "doc_next": ["next"]},
    )

    records = transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION)
    windows = sorted(
        (int(r["metadata"]["turn_start_index"]), int(r["metadata"]["turn_end_index"])) for r in records
    )

    assert windows == [(73, 85), (86, 92)]


def test_exact_duplicate_windows_deduped():
    rag = _FakeRetiredIndexBridge(
        docs=[
            _doc("doc_a", turn_start_index="5", turn_end_index="9", content_hash="sha256:same"),
            _doc("doc_b", turn_start_index="5", turn_end_index="9", content_hash="sha256:same"),
        ],
        contents={"doc_a": ["a"], "doc_b": ["b"]},
    )

    records = transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION)

    assert len(records) == 1


def test_non_conversation_document_is_skipped():
    rag = _FakeRetiredIndexBridge(
        docs=[_doc("doc_1", type="project_memory", result_type="project_memory")],
        contents={"doc_1": ["x"]},
    )

    assert transcript_memory_records_from_retired_index_bridge(rag, ["ds_1"], session_id_hash=SESSION) == []


def test_no_dataset_ids_returns_empty():
    rag = _FakeRetiredIndexBridge(docs=[_doc("doc_1")], contents={"doc_1": ["x"]})

    assert transcript_memory_records_from_retired_index_bridge(rag, [], session_id_hash=SESSION) == []
