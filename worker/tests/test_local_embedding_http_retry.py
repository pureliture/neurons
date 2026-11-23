"""Deterministic transport-level tests for local embedding HTTP retry classification."""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from agent_knowledge.local_embedding import CanonicalLocalEmbeddingClient, LocalEmbeddingClientError


def _http_error(status: int) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": f"http_{status}", "message": "boom"}}).encode()
    return urllib.error.HTTPError(
        "http://example/v1/embeddings", status, "err", hdrs={}, fp=io.BytesIO(body)
    )


@pytest.mark.parametrize("status,retryable", [(502, True), (500, True), (504, True), (503, True), (429, True), (400, False), (401, False), (413, False)])
def test_urllib_transport_retry_classification(status, retryable, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=15.0):
        calls["n"] += 1
        raise _http_error(status)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:9/v1",
        use_openai_sdk=False,
        max_retries=2,
        sleep_fn=lambda _s: None,
    )
    try:
        with pytest.raises(LocalEmbeddingClientError) as exc:
            client.embed_document("hello world")
        assert exc.value.retryable is retryable
        assert exc.value.status_code == status
        assert exc.value.stage == "batch_http"
        expected_calls = 3 if retryable else 1
        assert calls["n"] == expected_calls
    finally:
        client.close()


class _FakeSDKClient:
    def __init__(self, status):
        self.status = status
        self.calls = 0
        self.embeddings = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls += 1
        exc = Exception("sdk failure")
        exc.status_code = self.status
        exc.body = {"error": {"code": f"http_{self.status}"}}
        raise exc


@pytest.mark.parametrize("status,retryable", [(502, True), (500, True), (504, True), (503, True), (429, True), (400, False), (401, False), (413, False)])
def test_sdk_transport_retry_classification(status, retryable):
    fake = _FakeSDKClient(status)
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:9/v1",
        use_openai_sdk=True,
        openai_client=fake,
        max_retries=2,
        sleep_fn=lambda _s: None,
    )
    try:
        with pytest.raises(LocalEmbeddingClientError) as exc:
            client.embed_document("hello world")
        assert exc.value.retryable is retryable
        assert exc.value.status_code == status
        assert exc.value.stage == "batch_http"
        expected_calls = 3 if retryable else 1
        assert fake.calls == expected_calls
    finally:
        client.close()


def test_sdk_recovers_after_transient_502():
    class RecoveringSDKClient:
        def __init__(self):
            self.calls = 0
            self.embeddings = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                class SyntheticSDKError(Exception):
                    status_code = 502
                    body = {"error": {"code": "http_502"}}
                raise SyntheticSDKError("synthetic outage")
            return SimpleNamespace(
                model=kwargs["model"],
                data=[SimpleNamespace(index=0, embedding=[1.0] + [0.0] * 255)],
                usage=SimpleNamespace(prompt_tokens=1),
            )

    fake = RecoveringSDKClient()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:9/v1", use_openai_sdk=True,
        openai_client=fake, max_retries=2, sleep_fn=lambda _s: None,
    )
    try:
        result = client.embed_document("synthetic input")
        assert fake.calls == 2
        assert len(result.vector) == 256
        assert result.vector[0] != 0
    finally:
        client.close()


@pytest.mark.parametrize("status,retryable", [(502, True), (503, True), (429, True), (400, False), (413, False)])
def test_async_sdk_transport_retry_classification(status, retryable):
    class SyntheticSDKError(Exception):
        def __init__(self):
            super().__init__("synthetic failure")
            self.status_code = status
            self.body = {"error": {"code": f"http_{status}"}}

    class AsyncSDKClient:
        def __init__(self):
            self.calls = 0
            self.embeddings = SimpleNamespace(create=self.create)

        async def create(self, **kwargs):
            self.calls += 1
            raise SyntheticSDKError()

    fake = AsyncSDKClient()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:9/v1", use_openai_sdk=True,
        async_openai_client=fake, max_retries=2, retry_backoff_seconds=0,
    )
    try:
        with pytest.raises(LocalEmbeddingClientError) as exc:
            asyncio.run(client.aembed_document("synthetic input"))
        assert exc.value.retryable is retryable
        assert exc.value.status_code == status
        assert exc.value.stage == "batch_http"
        assert fake.calls == (3 if retryable else 1)
    finally:
        client.close()
