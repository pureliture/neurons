from __future__ import annotations

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.ragflow_fallback import reconstruct_sessions, RAGFLOW_FALLBACK_STATUS
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore


class _FakeReader:
    """Stands in for RagflowReader: returns a canned session->docs map and bodies."""

    def __init__(self, sid_docs, sid_meta, bodies):
        self._sid_docs = sid_docs
        self._sid_meta = sid_meta
        self._bodies = bodies  # doc_id -> body text

    def session_doc_map(self, want, *, max_pages=300):
        sd = {s: d for s, d in self._sid_docs.items() if s in want}
        sm = {s: self._sid_meta[s] for s in sd}
        return sd, sm

    def doc_body(self, doc_id, *, max_chunks=200):
        return self._bodies.get(doc_id, "")


def test_reconstruct_flags_ragflow_fallback_and_writes_clean():
    sid = dm.build_session_id_hash("claude", "gone-1")
    reader = _FakeReader(
        sid_docs={sid: ["d1", "d2"]},
        sid_meta={sid: ("claude", "-Users-ddalkak-Projects-neurons")},  # polluted slug
        bodies={"d1": "user asked about retirement", "d2": "assistant explained the gate"},
    )
    store = InMemoryCouchDBSourceStore()
    rep = reconstruct_sessions(session_hashes=[sid], reader=reader, store=store)
    assert rep["reconstructed"] == 1
    assert rep["chunks_written"] == 2
    cov = store.get(dm.coverage_manifest_doc_id(sid))
    pa = cov["project_authority"]
    assert pa["source"] == RAGFLOW_FALLBACK_STATUS
    assert pa["ambiguous"] is True
    assert pa["eligible_for_retirement"] is False
    # polluted path-slug canonicalized to the repo label
    assert cov["project"] == "neurons"
    sess = store.get(dm.session_doc_id(sid))
    assert sess["source_status"] == RAGFLOW_FALLBACK_STATUS



def test_reconstruct_reports_no_content():
    sid = dm.build_session_id_hash("gemini", "empty-1")
    reader = _FakeReader(sid_docs={sid: ["d1"]}, sid_meta={sid: ("gemini", "x")}, bodies={"d1": ""})
    store = InMemoryCouchDBSourceStore()
    rep = reconstruct_sessions(session_hashes=[sid], reader=reader, store=store)
    assert rep["no_content"] == 1
    assert rep["reconstructed"] == 0
