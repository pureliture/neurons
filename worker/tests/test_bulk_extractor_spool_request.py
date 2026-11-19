"""Real extractor serialization with synthetic post_fn and spooled full input."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_knowledge.llm_brain_core import bulk_full_input_neo4j_writer
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core import bulk_stream_runner
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkRedirectRejected,
    BulkRequestLimitExceeded,
    BulkSemanticSessionInput,
    OpenAICompatibleBulkSemanticExtractor,
    OpenAICompatibleEmbeddingBatcher,
    _urllib_post,
    _urllib_post_no_redirects,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode
from test_bulk_spool_privacy_boundary import FakeSpool


PAGES = ["FRONT-POSITION", "MIDDLE-POSITION", "END-POSITION"]


def _input(key: str, text) -> BulkSemanticSessionInput:
    episode = OntologyEpisode.from_payload(
        event_id="evt-" + key,
        natural_id="nat-" + key,
        entity_type="Session",
        payload={"project": "synthetic-project", "provider": "synthetic-provider"},
        observed_at="2026-01-01T00:00:00Z",
        reference_time="2026-01-01T00:00:00Z",
    )
    return BulkSemanticSessionInput(session_key=key, episode=episode, text=text)

def _post(captured: dict):
    def _fn(url, *, headers, body, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        request_bytes = b"".join(body.iter_bytes())
        captured["request_bytes"] = request_bytes
        captured["json"] = json.loads(request_bytes)
        return json.dumps({"choices": [{"message": {"content": json.dumps({
            "sessions": [{
                "session_key": key,
                "entities": [{"name": "Synthetic", "type": "Concept", "summary": "s"}],
                "relations": [],
            } for key in captured["keys"]]
        })}}]})
    return _fn

def test_real_extractor_serializes_spool_positions_and_evidence_without_truncation():
    spool = FakeSpool(PAGES)
    captured: dict = {
        "keys": ["session-conversation", "session-evidence"],
    }
    extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://synthetic.invalid/v1", model="synthetic-model",
        api_key="synthetic-key", post_fn=_post(captured),
    )
    batch = [
        _input("session-conversation", spool),
        _input("session-evidence", "EVIDENCE-BODY"),
    ]

    result = extractor.extract(batch)

    assert {item.session_key for item in result.sessions} == set(captured["keys"])
    user_content = captured["json"]["messages"][1]["content"]
    sessions = json.loads(user_content)["sessions"]
    by_key = {item["session_key"]: item for item in sessions}
    assert by_key["session-conversation"]["text"] == "".join(PAGES)
    assert by_key["session-evidence"]["text"] == "EVIDENCE-BODY"
    request_bytes = captured["request_bytes"]
    assert "FRONT-POSITION" in request_bytes.decode("utf-8")
    assert "MIDDLE-POSITION" in request_bytes.decode("utf-8")
    assert "END-POSITION" in request_bytes.decode("utf-8")
    assert "EVIDENCE-BODY" in request_bytes.decode("utf-8")
    evidence = extractor.last_client_serialization
    assert evidence["request_digest"] == "sha256:" + hashlib.sha256(request_bytes).hexdigest()
    assert evidence["sessions"]["session-conversation"]["text_digest"] == (
        "sha256:" + hashlib.sha256("".join(PAGES).encode("utf-8")).hexdigest()
    )
    assert evidence["sessions"]["session-evidence"]["text_digest"] == (
        "sha256:" + hashlib.sha256(b"EVIDENCE-BODY").hexdigest()
    )
    assert evidence["evidence_scope"] == "client_serialized_only"


def test_real_extractor_serializes_utf8_emoji_and_special_chars_across_pages():
    pages = ['한글-"인용"\\경로\n', "중간 😀😀 ", "끝-줄바꿈\t완료"]
    spool = FakeSpool(pages)
    captured: dict = {"keys": ["session-utf8"]}
    extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://127.0.0.1:9/v1",
        model="synthetic-model",
        api_key="synthetic-key",
        post_fn=_post(captured),
    )

    result = extractor.extract([_input("session-utf8", spool)])

    assert len(result.sessions) == 1
    sessions = json.loads(captured["json"]["messages"][1]["content"])["sessions"]
    assert sessions[0]["text"] == "".join(pages)
    assert extractor.last_client_serialization["sessions"]["session-utf8"]["text_bytes"] == len(
        "".join(pages).encode("utf-8")
    )


def test_real_extractor_streams_spool_to_real_loopback_http_server():
    pages = ["LOOPBACK-FRONT ", "가나다-😀 ", "LOOPBACK-END"]
    spool = FakeSpool(pages)
    server_seen: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            server_seen["path"] = self.path
            server_seen["content_length"] = length
            server_seen["raw"] = raw
            server_seen["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            outer = json.loads(raw.decode("utf-8"))
            inner = json.loads(outer["messages"][1]["content"])
            server_seen["sessions"] = inner["sessions"]
            payload = json.dumps({
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "sessions": [{
                                "session_key": item["session_key"],
                                "entities": [{"name": "LoopbackConcept", "type": "Concept", "summary": "ok"}],
                                "relations": [],
                            } for item in inner["sessions"]]
                        })
                    }
                }]
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        extractor = OpenAICompatibleBulkSemanticExtractor(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="synthetic-model",
            api_key="synthetic-key",
        )
        result = extractor.extract([
            _input("session-loopback", spool),
            _input("session-evidence", "EVIDENCE-LOOPBACK"),
        ])
        assert {item.session_key for item in result.sessions} == {
            "session-loopback", "session-evidence",
        }
        assert server_seen["path"] == "/v1/chat/completions"
        assert server_seen["digest"] == extractor.last_client_serialization["request_digest"]
        assert server_seen["content_length"] == extractor.last_client_serialization["request_bytes"]
        assert extractor.last_client_serialization["evidence_scope"] == "client_serialized_only"
        by_key = {item["session_key"]: item["text"] for item in server_seen["sessions"]}
        assert by_key["session-loopback"] == "".join(pages)
        assert by_key["session-evidence"] == "EVIDENCE-LOOPBACK"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_extractor_request_limit_fails_closed_on_spooled_and_non_spooled_without_http():
    calls = 0

    def _never_called(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP post must not run when limit is exceeded")

    spool = FakeSpool(["A" * 1000, "B" * 1000])
    spooled_extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://127.0.0.1:9/v1",
        model="synthetic-model",
        post_fn=_never_called,
        max_streamed_request_bytes=512,
    )
    with pytest.raises(BulkRequestLimitExceeded) as exc_spooled:
        spooled_extractor.extract([_input("session-spooled-over", spool)])
    assert exc_spooled.value.limit_cause == "client_request_byte_limit_exceeded_split_required"
    assert spooled_extractor.last_client_serialization is None
    assert calls == 0

    plain_extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://127.0.0.1:9/v1",
        model="synthetic-model",
        post_fn=_never_called,
        max_streamed_request_bytes=512,
    )
    with pytest.raises(BulkRequestLimitExceeded):
        plain_extractor.extract([_input("session-plain-over", "C" * 2000)])
    assert plain_extractor.last_client_serialization is None
    assert calls == 0


def test_full_input_no_redirect_blocks_301_302_303_307_308_cross_host_for_plain_and_streamed():
    target_requests: list[tuple[str, str | None, bool, bool, int]] = []
    initial_requests: list[tuple[str, str, int]] = []
    redirect_state = {"code": 301, "location": ""}

    class _TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            target_requests.append((
                "GET",
                self.headers.get("Host"),
                self.headers.get("Authorization") is not None,
                self.headers.get("x-opencode-session") is not None,
                0,
            ))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            target_requests.append((
                "POST",
                self.headers.get("Host"),
                self.headers.get("Authorization") is not None,
                self.headers.get("x-opencode-session") is not None,
                len(body),
            ))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            return

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            initial_requests.append(("POST", self.path, len(raw)))
            self.send_response(redirect_state["code"])
            self.send_header("Location", redirect_state["location"])
            self.end_headers()

        def log_message(self, *args):
            return

    target_srv = HTTPServer(("127.0.0.1", 0), _TargetHandler)
    redirect_srv = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    target_thread = threading.Thread(target=target_srv.serve_forever, daemon=True)
    redirect_thread = threading.Thread(target=redirect_srv.serve_forever, daemon=True)
    target_thread.start()
    redirect_thread.start()
    try:
        extractor = OpenAICompatibleBulkSemanticExtractor(
            base_url=f"http://127.0.0.1:{redirect_srv.server_port}/v1",
            model="hy4-flash-free",
            api_key="synthetic-key",
            fallback_models=("beam-free",),
            disallow_redirects=True,
        )
        assert extractor._disallow_redirects is True
        assert extractor._post_fn is _urllib_post_no_redirects

        for mode in ("plain", "streamed"):
            for code in (301, 302, 303, 307, 308):
                redirect_state["code"] = code
                redirect_state["location"] = f"http://localhost:{target_srv.server_port}/leaked"
                initial_requests.clear()
                target_requests.clear()
                payload = "PLAIN-SYNTHETIC" if mode == "plain" else FakeSpool(["STREAMED-", "SYNTHETIC"])
                with pytest.raises(BulkRedirectRejected) as exc_info:
                    extractor.extract([_input(f"sess-{mode}-{code}", payload)])
                assert exc_info.value.status_code == code
                # Initial host validates before connect, then receives the single first POST to emit 3xx;
                # redirect target must receive 0 requests, 0 Authorization, 0 x-opencode-session, and 0 body bytes.
                assert len(initial_requests) == 1
                assert initial_requests[0][0] == "POST"
                assert target_requests == []

        # Link-local metadata redirect target is also rejected without following.
        redirect_state["code"] = 302
        redirect_state["location"] = "http://169.254.169.254/latest/meta-data/"
        initial_requests.clear()
        target_requests.clear()
        with pytest.raises(BulkRedirectRejected) as metadata_exc:
            extractor.extract([_input("sess-metadata-redirect", FakeSpool(["META-CHECK"]))])
        assert metadata_exc.value.status_code == 302
        assert len(initial_requests) == 1
        assert target_requests == []
    finally:
        redirect_srv.shutdown()
        redirect_srv.server_close()
        target_srv.shutdown()
        target_srv.server_close()
        redirect_thread.join(timeout=5)
        target_thread.join(timeout=5)


def test_full_input_cli_wires_no_redirect_while_legacy_consumers_keep_default(monkeypatch, tmp_path):
    base_env = {
        "LLM_BRAIN_LLM_BASE_URL": "https://opencode.ai/zen/go/v1",
        "LLM_BRAIN_LLM_MODEL": "hy4-flash-free",
        "LLM_BRAIN_LLM_API_KEY": "synthetic-key",
    }
    legacy_extractor = OpenAICompatibleBulkSemanticExtractor.from_env(base_env)
    assert legacy_extractor._disallow_redirects is False
    assert legacy_extractor._post_fn is _urllib_post

    full_input_extractor = OpenAICompatibleBulkSemanticExtractor.from_env(
        base_env, disallow_redirects=True
    )
    assert full_input_extractor._disallow_redirects is True
    assert full_input_extractor._post_fn is _urllib_post_no_redirects

    batcher = OpenAICompatibleEmbeddingBatcher(
        base_url="http://127.0.0.1:8080/v1", model="text-embedding-synthetic"
    )
    assert batcher._post_fn is _urllib_post

    recorded_stream: list[tuple[dict[str, object], OpenAICompatibleBulkSemanticExtractor]] = []
    real_from_env = OpenAICompatibleBulkSemanticExtractor.from_env

    @classmethod  # type: ignore[misc]
    def _spy_from_env(cls, environ=None, **kwargs):
        instance = real_from_env.__func__(cls, environ, **kwargs)
        recorded_stream.append((dict(kwargs), instance))
        return instance

    class _FakeWriter:
        def __init__(self, *args, **kwargs):
            return

        def close(self) -> None:
            return

    captured_extractors: list[OpenAICompatibleBulkSemanticExtractor] = []
    monkeypatch.setattr(OpenAICompatibleBulkSemanticExtractor, "from_env", _spy_from_env)
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: object())
    monkeypatch.setattr(bulk_full_input_neo4j_writer, "Neo4jShadowWriter", _FakeWriter)
    monkeypatch.delenv("TRIAL_PG_DSN", raising=False)
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("TRIAL_NEO4J_URI", "bolt://127.0.0.1:7687")
    monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", "synthetic-pw")
    monkeypatch.setenv("LLM_BRAIN_LLM_BASE_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.setenv("LLM_BRAIN_LLM_MODEL", "hy4-flash-free")
    monkeypatch.setenv("LLM_BRAIN_LLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "hy4-flash-free")

    def _fake_stream_runner(*, extractor, **kwargs):
        captured_extractors.append(extractor)
        return {"status": "ok"}

    monkeypatch.setattr(bulk_stream_runner, "run_streamed_full_input", _fake_stream_runner)
    rc = cli.main([
        "--ledger", str(tmp_path / "ledger.db"),
        "--runtime-dir", str(tmp_path / "runtime"),
        "--stream-mode",
        "--trial-neo4j-writer",
        "--full-input-policy-version", "policy-redirect-test",
    ])
    assert rc == 0
    assert len(recorded_stream) == 1
    assert recorded_stream[0][0] == {"disallow_redirects": True}
    assert len(captured_extractors) == 1
    assert captured_extractors[0]._disallow_redirects is True
    assert captured_extractors[0]._post_fn is _urllib_post_no_redirects

    # Legacy non-full-input run calls from_env() without disallow_redirects override.
    recorded_stream.clear()
    monkeypatch.setattr(cli, "_acquire_runtime_lock", lambda *a, **kw: ("ok", None))
    monkeypatch.setattr(cli, "_release_runtime_lock", lambda *a, **kw: None)
    monkeypatch.setattr(
        cli,
        "_select_sessions_bounded",
        lambda *a, **kw: type("_Sel", (), {"sessions": [], "source_count": 0, "eligible_count": 0})(),
    )
    monkeypatch.setattr(cli.DeterministicGraphitiSemanticWriter, "from_env", classmethod(lambda cls: _FakeWriter()))
    legacy_report = cli.run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "legacy.db",
        source_store=object(),
        limit=0,
        runtime_dir=tmp_path / "rt-legacy",
        extractor=None,
        writer=None,
        full_input_policy_version="",
    )
    assert legacy_report["status"] == "ok"
    assert len(recorded_stream) == 1
    assert recorded_stream[0][0] == {}
    assert recorded_stream[0][1]._disallow_redirects is False
    assert recorded_stream[0][1]._post_fn is _urllib_post

    # Non-stream full-input policy mode also upgrades default extractor to no-redirect.
    recorded_stream.clear()
    full_report = cli.run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "full_nonstream.db",
        source_store=object(),
        limit=0,
        runtime_dir=tmp_path / "rt-full",
        extractor=None,
        writer=None,
        full_input_policy_version="policy-nonstream-v1",
    )
    assert full_report["status"] == "ok"
    assert len(recorded_stream) == 1
    assert recorded_stream[0][0] == {"disallow_redirects": True}
    assert recorded_stream[0][1]._disallow_redirects is True
    assert recorded_stream[0][1]._post_fn is _urllib_post_no_redirects


def test_trial_env_fallback_precedence_blank_safety_and_non_secret_errors(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_knowledge.llm_brain_core.bulk_full_input import (
        resolve_trial_neo4j_config,
        resolve_trial_pg_dsn,
    )
    from agent_knowledge.llm_brain_core.shadow_search_adapter import (
        ShadowSearchAdapter,
        build_shadow_search_adapter_from_env,
    )

    # 1) resolve_trial_pg_dsn: TRIAL_PG_DSN > NEURON_LEDGER_PG_DSN; whitespace falls back safely.
    assert resolve_trial_pg_dsn({
        "TRIAL_PG_DSN": "postgresql://t:tpw@127.0.0.1:5432/t",
        "NEURON_LEDGER_PG_DSN": "postgresql://o:opw@127.0.0.1:5432/o",
    }) == "postgresql://t:tpw@127.0.0.1:5432/t"
    assert resolve_trial_pg_dsn({
        "TRIAL_PG_DSN": "   ",
        "NEURON_LEDGER_PG_DSN": "  postgresql://o:opw@127.0.0.1:5432/o  ",
    }) == "postgresql://o:opw@127.0.0.1:5432/o"
    assert resolve_trial_pg_dsn({}) == ""

    # 2) resolve_trial_neo4j_config: TRIAL_* > LLM_BRAIN_NEO4J_* > missing raises without secrets.
    assert resolve_trial_neo4j_config({
        "TRIAL_NEO4J_URI": "bolt://127.0.0.1:17687",
        "TRIAL_NEO4J_PASSWORD": "trial-only-secret-xyz",
        "LLM_BRAIN_NEO4J_URI": "bolt://127.0.0.1:7687",
        "LLM_BRAIN_NEO4J_PASSWORD": "ops-only-secret-abc",
    }) == ("bolt://127.0.0.1:17687", "neo4j", "trial-only-secret-xyz")
    assert resolve_trial_neo4j_config({
        "TRIAL_NEO4J_URI": "  ",
        "TRIAL_NEO4J_USER": "",
        "TRIAL_NEO4J_PASSWORD": "",
        "LLM_BRAIN_NEO4J_URI": "bolt://127.0.0.1:7687",
        "LLM_BRAIN_NEO4J_USER": "opsuser",
        "LLM_BRAIN_NEO4J_PASSWORD": "ops-only-secret-abc",
    }) == ("bolt://127.0.0.1:7687", "opsuser", "ops-only-secret-abc")
    with pytest.raises(RuntimeError) as exc_info:
        resolve_trial_neo4j_config({
            "LLM_BRAIN_NEO4J_URI": "bolt://127.0.0.1:7687",
            "LLM_BRAIN_NEO4J_PASSWORD": "   ",
        })
    assert "ops-only-secret-abc" not in str(exc_info.value)
    assert "TRIAL_NEO4J_PASSWORD/LLM_BRAIN_NEO4J_PASSWORD" in str(exc_info.value)

    # 3) cli.main --stream-mode uses ops fallback when TRIAL_* absent and calls PgChunkState + Neo4jShadowWriter.
    seen_writer_args: list[tuple[str, str, str]] = []
    seen_pg_dsns: list[str] = []

    class _SpyWriter:
        def __init__(self, uri: str, user: str, password: str) -> None:
            seen_writer_args.append((uri, user, password))

        def close(self) -> None:
            return

    class _SpyPgState:
        def __init__(self, dsn: str) -> None:
            seen_pg_dsns.append(dsn)

    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: object())
    monkeypatch.setattr(bulk_full_input_neo4j_writer, "Neo4jShadowWriter", _SpyWriter)
    monkeypatch.setattr(
        "agent_knowledge.llm_brain_core.bulk_full_input_pg_state.PgChunkState",
        _SpyPgState,
    )
    monkeypatch.setattr(bulk_stream_runner, "run_streamed_full_input", lambda **kwargs: {"status": "ok"})
    for key in ("TRIAL_PG_DSN", "TRIAL_NEO4J_URI", "TRIAL_NEO4J_USER", "TRIAL_NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("NEURON_LEDGER_PG_DSN", "postgresql://ops:opspw@127.0.0.1:5432/opsdb")
    monkeypatch.setenv("LLM_BRAIN_NEO4J_URI", "bolt://127.0.0.1:7687")
    monkeypatch.setenv("LLM_BRAIN_NEO4J_PASSWORD", "ops-neo4j-pw")
    monkeypatch.setenv("LLM_BRAIN_LLM_BASE_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.setenv("LLM_BRAIN_LLM_MODEL", "hy4-flash-free")
    monkeypatch.setenv("LLM_BRAIN_LLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "hy4-flash-free")

    rc = cli.main([
        "--ledger", str(tmp_path / "ledger.db"),
        "--runtime-dir", str(tmp_path / "runtime"),
        "--stream-mode",
        "--trial-neo4j-writer",
        "--full-input-policy-version", "policy-ops-fallback",
    ])
    assert rc == 0
    assert seen_writer_args == [("bolt://127.0.0.1:7687", "neo4j", "ops-neo4j-pw")]
    assert seen_pg_dsns == ["postgresql://ops:opspw@127.0.0.1:5432/opsdb"]

    # 4) build_shadow_search_adapter_from_env supports NEURON_LEDGER_PG_DSN fallback and TRIAL_* precedence.
    legacy = object()
    store_no_dsn = SimpleNamespace()
    ops_only_env = {
        "NEURON_BULK_SHADOW_READ_ELIGIBLE": "1",
        "LLM_BRAIN_BULK_FULL_INPUT_POLICY_VERSION": "policy-v1",
        "NEURON_LEDGER_PG_DSN": "postgresql://ops:opspw@127.0.0.1:5432/opsdb",
        "LLM_BRAIN_NEO4J_URI": "bolt://127.0.0.1:7687",
        "LLM_BRAIN_NEO4J_PASSWORD": "ops-neo4j-pw",
        "COUCHDB_URL": "http://127.0.0.1:5984",
        "COUCHDB_DB": "synthetic_db",
        "COUCHDB_USER": "u",
        "COUCHDB_PASSWORD": "p",
    }
    adapter_ops = build_shadow_search_adapter_from_env(legacy, store_no_dsn, ops_only_env)
    assert isinstance(adapter_ops, ShadowSearchAdapter)
    assert adapter_ops.policy_version == "policy-v1"
    assert build_shadow_search_adapter_from_env(legacy, store_no_dsn, {**ops_only_env, "NEURON_LEDGER_PG_DSN": "  "}) is legacy
