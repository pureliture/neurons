"""Existing live durable ownership: validate, bind, never provision or reset."""
import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest

from agent_knowledge.rag_ingress.shadow_worker import IngestStateStore, run_consume


@pytest.mark.parametrize("drift", [None, "stream", "name", "durable", "filter", "filters", "push", "ack", "max_deliver", "missing", "permission"])
def test_live_consumer_bind_preserves_existing_progress(tmp_path, monkeypatch, drift):
    events = []
    config = SimpleNamespace(durable_name="existing", filter_subject="rag.ingress.>",
        filter_subjects=None, deliver_subject=None, ack_policy="explicit", max_deliver=5)
    info = SimpleNamespace(name="existing", config=config, ack_floor=61178,
        delivered=61178, num_pending=3, num_ack_pending=2)
    before = copy.deepcopy(info.__dict__)
    subjects = ["rag.ingress.>"]
    if drift == "stream": subjects = ["other.>"]
    if drift == "name": info.name = "other"
    if drift == "durable": config.durable_name = "other"
    if drift == "filter": config.filter_subject = "other.>"
    if drift == "filters": config.filter_subjects = ["other.>"]
    if drift == "push": config.deliver_subject = "push.inbox"
    if drift == "ack": config.ack_policy = "none"
    if drift == "max_deliver": config.max_deliver = 1

    class JetStream:
        async def stream_info(self, stream):
            events.append("stream_get")
            return SimpleNamespace(config=SimpleNamespace(subjects=subjects))
        async def consumer_info(self, stream, durable):
            events.append("consumer_get")
            if drift in {"missing", "permission"}: raise RuntimeError(drift)
            return info
        async def pull_subscribe_bind(self, *, consumer, stream):
            assert consumer == "existing" and stream == "RAG_INGRESS_QUEUE"
            events.append("bind")
            return object()
        async def pull_subscribe(self, *args, **kwargs):
            pytest.fail("must not create/update live consumer")
        async def delete_consumer(self, *args, **kwargs):
            pytest.fail("must not delete live consumer")
        async def add_stream(self, *args, **kwargs):
            pytest.fail("must not create live stream")

    class Connection:
        def jetstream(self): return JetStream()
        async def drain(self): events.append("drain")

    async def connect(url): return Connection()
    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))
    kwargs = dict(nats_url="nats://fixture", stream="RAG_INGRESS_QUEUE", subject="rag.ingress.>",
        durable="existing", store=IngestStateStore(tmp_path / "ingress.sqlite"), backend=None,
        deliver=False, max_messages=0, allow_live=True, log=lambda line: None)
    if drift is None:
        result = asyncio.run(run_consume(**kwargs))
        assert result["processed"] == 0
        assert events == ["stream_get", "consumer_get", "bind", "drain"]
        assert info.__dict__ == before
    else:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(run_consume(**kwargs))
        assert "bind" not in events
        assert events[-1] == "drain"
