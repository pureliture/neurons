import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agent_knowledge.couchdb_source import watch_cli
from agent_knowledge.session_memory import change_events as ce

H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def _event_bytes(session_id_hash, payload_hash="f" * 64):
    return ce.SessionChangedEvent(session_id_hash=session_id_hash, payload_hash=payload_hash).to_bytes()


class FakeMsg:
    def __init__(self, data, num_delivered=1):
        self.data = data
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.calls = []

    async def ack(self):
        self.calls.append(("ack", None))

    async def nak(self, delay=None):
        self.calls.append(("nak", delay))

    async def term(self):
        self.calls.append(("term", None))


class FakeSub:
    def __init__(self, batches):
        self.batches = list(batches)
        self.fetch_calls = []

    async def fetch(self, count, timeout):
        self.fetch_calls.append((count, timeout))
        if self.batches:
            batch = self.batches.pop(0)
            if batch:
                return batch
        raise TimeoutError("idle")


class FakeJs:
    def __init__(self, sub, *, dlq_fail=False):
        self.sub = sub
        self.published = []
        self.dlq_fail = dlq_fail
        self.consumer_config = None

    async def stream_info(self, stream):
        return {"name": stream}

    async def pull_subscribe(self, subject, *, durable, stream, config):
        self.consumer_config = (subject, durable, stream, config)
        return self.sub

    async def publish(self, subject, payload, *, stream=None, headers=None):
        if self.dlq_fail:
            raise ConnectionError("dlq down")
        self.published.append((subject, json.loads(payload), stream))


class FakeNc:
    def __init__(self, js):
        self.js = js
        self.drained = False

    def jetstream(self):
        return self.js

    async def drain(self):
        self.drained = True


def _settings(tmp_path, **env):
    base = {
        "SESSION_MEMORY_WATCH_HEARTBEAT": str(tmp_path / "hb.json"),
        "SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS": "0",
        "SESSION_MEMORY_WATCH_STARTUP_SWEEP": "0",
        "SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS": "0",
        "SESSION_MEMORY_WATCH_FETCH_WAIT_SECONDS": "1",
    }
    base.update(env)
    return watch_cli.WatchSettings(base), base


def _run(settings, env, js, run_build, max_cycles, **kw):
    nc = FakeNc(js)

    async def connect(_url):
        return nc

    counters = asyncio.run(
        watch_cli.run_consume_loop(
            settings=settings, environ=env, run_build=run_build, connect=connect,
            log=lambda _l: None, max_cycles=max_cycles, **kw,
        )
    )
    assert nc.drained
    return counters


def test_settings_defaults_match_design():
    s = watch_cli.WatchSettings({})
    assert (s.debounce_seconds, s.debounce_max_seconds, s.idle_sweep_seconds) == (2, 10, 900)
    assert s.max_deliver == 5
    assert s.nak_backoff_seconds == (30, 60, 120, 300)
    assert s.ack_wait_seconds == 300 + 60
    assert s.durable == "session_memory_projector"
    assert s.subject == "session_memory.changed"
    assert s.startup_sweep is True


def test_classify_and_backoff_rules():
    event = ce.SessionChangedEvent(session_id_hash=H1)
    outcomes = {H1: {"status": "projected"}}
    assert watch_cli.classify_message(event, rc=0, outcomes=outcomes) == ("ack", "")
    assert watch_cli.classify_message(event, rc=0, outcomes={H1: {"status": "pending"}}) == ("ack", "")
    assert watch_cli.classify_message(event, rc=1, outcomes={H1: {"status": "failed", "error_class": "X"}}) == ("nak", "X")
    assert watch_cli.classify_message(event, rc=124, outcomes={}) == ("nak", "build_rc_124")
    assert watch_cli.classify_message(event, rc=0, outcomes={}) == ("nak", "missing_session_report")
    assert [watch_cli.nak_delay_seconds(n, (30, 60, 120, 300)) for n in (1, 2, 3, 4, 9)] == [30, 60, 120, 300, 300]
    assert watch_cli.parse_build_report("noise\n{\"a\": 1}\n{\"sessions\": []}\n") == {"sessions": []}
    assert watch_cli.parse_build_report("") == {}


def test_batch_dedups_sessions_and_acks_projected_and_pending(tmp_path):
    m1, m2, m3 = FakeMsg(_event_bytes(H1, "a" * 64)), FakeMsg(_event_bytes(H1, "b" * 64)), FakeMsg(_event_bytes(H2))
    sub = FakeSub([[m1, m2, m3]])
    js = FakeJs(sub)
    settings, env = _settings(tmp_path)
    builds = []

    def run_build(ids):
        builds.append(ids)
        return 0, {"sessions": [{"session_id_hash": H1, "status": "projected"}, {"session_id_hash": H2, "status": "pending"}]}

    counters = _run(settings, env, js, run_build, max_cycles=1)

    assert builds == [(H1, H2)]
    assert [m.calls for m in (m1, m2, m3)] == [[("ack", None)]] * 3
    assert counters["acked"] == 3 and counters["event_cycles"] == 1
    subject, durable, stream, config = js.consumer_config
    assert (subject, durable, stream) == ("session_memory.changed", "session_memory_projector", "SESSION_MEMORY_EVENTS")
    assert config.max_deliver == 5 and config.ack_wait == 360.0 and config.max_ack_pending == 10
    heartbeat = json.loads(Path(env["SESSION_MEMORY_WATCH_HEARTBEAT"]).read_text())
    assert heartbeat["phase"] == "stopped" and heartbeat["last_reason"] == "changes" and heartbeat["last_build_rc"] == 0
    assert H1 not in json.dumps(heartbeat)


def test_failed_session_is_naked_with_backoff_then_dead_lettered_at_max_deliver(tmp_path):
    first = FakeMsg(_event_bytes(H1), num_delivered=2)
    last = FakeMsg(_event_bytes(H1), num_delivered=5)
    sub = FakeSub([[first], [last]])
    js = FakeJs(sub)
    settings, env = _settings(tmp_path)

    def run_build(ids):
        return 1, {"sessions": [{"session_id_hash": H1, "status": "failed", "error_class": "RuntimeError"}]}

    counters = _run(settings, env, js, run_build, max_cycles=2)

    assert first.calls == [("nak", 60)]
    assert last.calls == [("term", None)]
    assert counters["naked"] == 1 and counters["dead_lettered"] == 1 and counters["consecutive_failures"] == 2
    (subject, body, stream), = js.published
    assert subject == "session_memory.dead_letter" and stream == "SESSION_MEMORY_EVENTS"
    assert body["attempts"] == 5 and body["error_class"] == "RuntimeError" and body["event"]["session_id_hash"] == H1


def test_dead_letter_record_failure_preserves_message_with_nak(tmp_path):
    msg = FakeMsg(_event_bytes(H1), num_delivered=5)
    js = FakeJs(FakeSub([[msg]]), dlq_fail=True)
    settings, env = _settings(tmp_path)

    counters = _run(settings, env, js, lambda ids: (1, {"sessions": [{"session_id_hash": H1, "status": "failed"}]}), max_cycles=1)

    assert msg.calls == [("nak", 60)]
    assert counters["dead_lettered"] == 0 and counters["naked"] == 1


def test_invalid_event_is_dead_lettered_without_a_build(tmp_path):
    bad = FakeMsg(b"{\"session_id_hash\": \"raw-id\"}")
    good = FakeMsg(_event_bytes(H2))
    js = FakeJs(FakeSub([[bad, good]]))
    settings, env = _settings(tmp_path)
    builds = []

    def run_build(ids):
        builds.append(ids)
        return 0, {"sessions": [{"session_id_hash": H2, "status": "projected"}]}

    counters = _run(settings, env, js, run_build, max_cycles=1)

    assert builds == [(H2,)]
    assert bad.calls == [("term", None)] and good.calls == [("ack", None)]
    assert counters["terminated_invalid"] == 1
    assert js.published[0][1]["error_class"] == "invalid_event"


def test_debounce_extends_until_quiet_or_max_window(tmp_path):
    m1, m2 = FakeMsg(_event_bytes(H1)), FakeMsg(_event_bytes(H2))
    sub = FakeSub([[m1], [m2], []])
    js = FakeJs(sub)
    settings, env = _settings(tmp_path, SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS="2", SESSION_MEMORY_WATCH_DEBOUNCE_MAX_SECONDS="10")
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 0.5
        return clock["now"]

    builds = []
    counters = _run(settings, env, js, lambda ids: builds.append(ids) or (0, {"sessions": [{"session_id_hash": h, "status": "projected"}for h in ids]}), max_cycles=1, monotonic=monotonic)

    assert builds == [(H1, H2)]
    assert counters["acked"] == 2
    # first fetch waits up to the fetch window, debounce fetches wait <= 2s
    assert sub.fetch_calls[0][1] == 1.0
    assert all(t <= 2.0 for _, t in sub.fetch_calls[1:])


def test_startup_and_idle_sweep_run_regular_cycles_without_session_ids(tmp_path):
    sub = FakeSub([])
    js = FakeJs(sub)
    settings, env = _settings(tmp_path, SESSION_MEMORY_WATCH_STARTUP_SWEEP="1", SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS="900")
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 500.0
        return clock["now"]

    builds = []
    counters = _run(settings, env, js, lambda ids: builds.append(ids) or (0, {"sessions": []}), max_cycles=2, monotonic=monotonic)

    assert builds == [(), ()]
    assert counters["sweeps"] == 2 and counters["event_cycles"] == 0
    heartbeat = json.loads(Path(env["SESSION_MEMORY_WATCH_HEARTBEAT"]).read_text())
    assert heartbeat["last_reason"] == "idle_sweep"


def test_health_is_not_applicable_outside_watch_and_detects_stale_heartbeat(tmp_path, capsys):
    hb = tmp_path / "hb.json"
    assert watch_cli.main(["--health"], environ={"RUN_MODE": "cron"}) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_applicable"

    env = {"RUN_MODE": "watch", "SESSION_MEMORY_WATCH_HEARTBEAT": str(hb)}
    assert watch_cli.main(["--health"], environ=env) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "stale"

    watch_cli._write_heartbeat(str(hb), phase="waiting", consecutive_failures=2, dead_lettered=1)
    assert watch_cli.main(["--health"], environ=env) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["consecutive_failures"] == 2 and out["dead_lettered"] == 1


def test_consume_requires_existing_build_script(tmp_path, capsys):
    env = {"SESSION_MEMORY_BUILD_SCRIPT": str(tmp_path / "missing.sh"), "SESSION_MEMORY_WATCH_HEARTBEAT": str(tmp_path / "hb.json")}
    assert watch_cli.main(["--consume"], environ=env) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "env_missing"


def test_run_build_script_passes_session_ids_env_and_parses_report(tmp_path):
    script = tmp_path / "build.sh"
    script.write_text("#!/bin/sh\necho \"ids=${SESSION_MEMORY_BUILD_SESSION_IDS:-none}\"\necho '{\"sessions\": [], \"projected\": 0}'\nexit 0\n")
    script.chmod(0o755)
    settings = watch_cli.WatchSettings({"SESSION_MEMORY_BUILD_SCRIPT": str(script)})

    rc, report = watch_cli.run_build_script((H1, H2), settings=settings, environ={"PATH": "/usr/bin:/bin"})
    assert rc == 0 and report == {"sessions": [], "projected": 0}
    rc, report = watch_cli.run_build_script((), settings=settings, environ={"PATH": "/usr/bin:/bin", "SESSION_MEMORY_BUILD_SESSION_IDS": "stale"})
    assert rc == 0


def test_cli_registry_exposes_watch_command():
    from agent_knowledge import cli

    assert "couchdb-session-memory-watch" in cli.COMMAND_HANDLERS
    assert cli.COMMAND_METADATA["couchdb-session-memory-watch"]["runtime_category"] == "active_runtime"


def test_bare_hex_event_is_normalised_before_build_and_acked_against_stored_hash(tmp_path):
    bare_upper = H1.split(":", 1)[1].upper()
    raw = json.dumps({"session_id_hash": bare_upper, "schema_version": ce.SESSION_CHANGED_EVENT_SCHEMA}).encode()
    m1 = FakeMsg(raw)
    sub = FakeSub([[m1]])
    js = FakeJs(sub)
    settings, env = _settings(tmp_path)
    builds = []

    def run_build(ids):
        builds.append(ids)
        return 0, {"sessions": [{"session_id_hash": H1, "status": "projected"}]}

    counters = _run(settings, env, js, run_build, max_cycles=1)

    assert builds == [(H1,)]
    assert m1.calls == [("ack", None)]
    assert counters["acked"] == 1 and counters["terminated_invalid"] == 0 and js.published == []
