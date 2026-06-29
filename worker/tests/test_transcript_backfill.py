from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory import transcript_backfill as bf
from agent_knowledge.session_memory.transcript_backfill import (
    TranscriptBackfillConfig,
    TranscriptBackfillRunner,
)

SM = "ds_sm"
TX = "ds_tx"
PROJECT = "workspace-index-advisor"


def _sm(sid, *, status="1", run="DONE"):
    return {"id": "sm-" + sid, "status": status, "run": run, "meta_fields": {"session_id_hash": sid}}


def _tx(sid, *, doc_id=None):
    return {"id": doc_id or ("tx-" + sid), "meta_fields": {"session_id_hash": sid, "provider": "codex", "project": PROJECT}}


class _FakeBfClient:
    def __init__(self, *, sm_docs=None, tx_docs=None, **kw):
        self.sm_docs = list(sm_docs or [])
        self.tx_docs = list(tx_docs or [])

    def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
        docs = self.sm_docs if dataset_id == SM else (self.tx_docs if dataset_id == TX else [])
        start = (page - 1) * page_size
        return docs[start : start + page_size]


def _dirty_sessions(ledger_path):
    with Ledger(ledger_path)._connect() as connection:
        return {row[0] for row in connection.execute("SELECT session_id_hash FROM dirty_session_memory").fetchall()}


def _cfg(tmp_path, *, max_sessions=100):
    return TranscriptBackfillConfig(
        ledger_path=tmp_path / "l.sqlite",
        transcript_dataset_id=TX,
        session_memory_dataset_id=SM,
        index_url="http://localhost:9380",
        max_sessions=max_sessions,
    )


def _patch(monkeypatch, fake):
    monkeypatch.setattr(bf, "RetiredIndexBridgeHttpClient", lambda **k: fake)


def test_seeds_only_unsummarized_sessions(tmp_path, monkeypatch):
    fake = _FakeBfClient(sm_docs=[_sm("S1")], tx_docs=[_tx("S1"), _tx("S2"), _tx("S3")])
    _patch(monkeypatch, fake)
    cfg = _cfg(tmp_path)
    rep = TranscriptBackfillRunner(config=cfg, token="t").run()
    assert rep["summarized_session_count"] == 1
    assert rep["seeded_session_count"] == 2
    assert rep["mutation_performed"] is True
    assert rep["network_used"] is True
    assert rep["index_write_performed"] is False
    assert _dirty_sessions(cfg.ledger_path) == {"S2", "S3"}


def test_disabled_summary_session_is_reseeded(tmp_path, monkeypatch):
    fake = _FakeBfClient(sm_docs=[_sm("S4", status="0")], tx_docs=[_tx("S4")])
    _patch(monkeypatch, fake)
    cfg = _cfg(tmp_path)
    rep = TranscriptBackfillRunner(config=cfg, token="t").run()
    assert rep["summarized_session_count"] == 0
    assert rep["seeded_session_count"] == 1
    assert _dirty_sessions(cfg.ledger_path) == {"S4"}


def test_bounded_by_max_sessions(tmp_path, monkeypatch):
    fake = _FakeBfClient(sm_docs=[], tx_docs=[_tx("S1"), _tx("S2"), _tx("S3")])
    _patch(monkeypatch, fake)
    cfg = _cfg(tmp_path, max_sessions=2)
    rep = TranscriptBackfillRunner(config=cfg, token="t").run()
    assert rep["seeded_session_count"] == 2
    assert len(_dirty_sessions(cfg.ledger_path)) == 2


def test_distinct_sessions_deduped(tmp_path, monkeypatch):
    fake = _FakeBfClient(sm_docs=[], tx_docs=[_tx("S1", doc_id="a"), _tx("S1", doc_id="b"), _tx("S2", doc_id="c")])
    _patch(monkeypatch, fake)
    cfg = _cfg(tmp_path)
    rep = TranscriptBackfillRunner(config=cfg, token="t").run()
    assert rep["seeded_session_count"] == 2
    assert _dirty_sessions(cfg.ledger_path) == {"S1", "S2"}
