"""CLI entry point: couchdb-session-memory-watch.

Event-driven trigger for the session-memory build lane (``RUN_MODE=watch``).
``--consume`` runs a durable JetStream pull consumer on
``session_memory.changed`` (published by the ingress lane after each
authoritative CouchDB write) and turns each debounced batch into one
``build-once.sh`` cycle restricted to the changed sessions
(``SESSION_MEMORY_BUILD_SESSION_IDS`` -> ``--session-id-hash``). Delivery
retry and dead-lettering stay with JetStream: a session whose projection
failed is nak'd with backoff, and once ``max_deliver`` is reached the event is
copied to ``session_memory.dead_letter`` and terminated.

The selection checkpoint is never touched by event cycles; the periodic idle
safety sweep (and the start-up sweep) run the regular incremental cycle, which
remains the checkpoint's single writer and covers writes that bypassed ingress
(backfill/replay) or whose publish failed.

Exit codes for ``--consume``: 1 transport failure (the entrypoint backs off and
restarts), 2 configuration error. ``--health`` reads the heartbeat file for a
compose healthcheck.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping

from ..session_memory.change_events import (
    SESSION_MEMORY_CHANGED_SUBJECT,
    SESSION_MEMORY_DEAD_LETTER_SUBJECT,
    SESSION_MEMORY_EVENTS_STREAM,
    SESSION_MEMORY_PROJECTOR_DURABLE,
    SessionChangedEvent,
    build_dead_letter_payload,
    ensure_session_memory_stream,
    nats_url_from_env,
    parse_session_changed_event,
)

WATCH_CLI_SCHEMA_VERSION = "couchdb_session_memory_watch.v2"
HEARTBEAT_SCHEMA_VERSION = "session_memory_watch_heartbeat.v1"
DEFAULT_HEARTBEAT_PATH = "state/runtime/session-memory-watch.json"
DEFAULT_BUILD_SCRIPT = "/app/deploy/build-once.sh"

DEFAULT_DEBOUNCE_SECONDS = 2
DEFAULT_DEBOUNCE_MAX_SECONDS = 10
DEFAULT_IDLE_SWEEP_SECONDS = 900
DEFAULT_FETCH_WAIT_SECONDS = 30
DEFAULT_MAX_DELIVER = 5
DEFAULT_NAK_BACKOFF_SECONDS = (30, 60, 120, 300)
DEFAULT_BUILD_LIMIT = 10
DEFAULT_BUILD_TIMEOUT_SECONDS = 300
ACK_STATUSES = frozenset({"projected", "pending", "skipped"})


def _positive_int(value: object, default: int) -> int:
    text = str(value if value is not None else "").strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return default


def _non_negative_int(value: object, default: int) -> int:
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return int(text)
    return default


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_backoff(value: object) -> tuple[int, ...]:
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if parts and all(p.isdigit() and int(p) > 0 for p in parts):
        return tuple(int(p) for p in parts)
    return DEFAULT_NAK_BACKOFF_SECONDS


class WatchSettings:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self.nats_url = nats_url_from_env(environ)
        self.stream = str(environ.get("SESSION_MEMORY_EVENTS_STREAM") or SESSION_MEMORY_EVENTS_STREAM)
        self.subject = str(environ.get("SESSION_MEMORY_EVENTS_SUBJECT") or SESSION_MEMORY_CHANGED_SUBJECT)
        self.dead_letter_subject = str(
            environ.get("SESSION_MEMORY_EVENTS_DEAD_LETTER_SUBJECT") or SESSION_MEMORY_DEAD_LETTER_SUBJECT
        )
        self.durable = str(environ.get("SESSION_MEMORY_WATCH_DURABLE") or SESSION_MEMORY_PROJECTOR_DURABLE)
        self.debounce_seconds = _non_negative_int(
            environ.get("SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS"), DEFAULT_DEBOUNCE_SECONDS
        )
        self.debounce_max_seconds = _non_negative_int(
            environ.get("SESSION_MEMORY_WATCH_DEBOUNCE_MAX_SECONDS"), DEFAULT_DEBOUNCE_MAX_SECONDS
        )
        # 0 disables the safety sweep.
        self.idle_sweep_seconds = _non_negative_int(
            environ.get("SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS"), DEFAULT_IDLE_SWEEP_SECONDS
        )
        self.startup_sweep = str(environ.get("SESSION_MEMORY_WATCH_STARTUP_SWEEP") or "1").strip() != "0"
        self.fetch_wait_seconds = _positive_int(
            environ.get("SESSION_MEMORY_WATCH_FETCH_WAIT_SECONDS"), DEFAULT_FETCH_WAIT_SECONDS
        )
        self.max_deliver = _positive_int(environ.get("SESSION_MEMORY_WATCH_MAX_DELIVER"), DEFAULT_MAX_DELIVER)
        self.nak_backoff_seconds = _parse_backoff(environ.get("SESSION_MEMORY_WATCH_NAK_BACKOFF_SECONDS"))
        self.build_limit = _positive_int(environ.get("SESSION_MEMORY_BUILD_LIMIT"), DEFAULT_BUILD_LIMIT)
        self.build_timeout_seconds = _positive_int(
            environ.get("SESSION_MEMORY_BUILD_TIMEOUT_SECONDS"), DEFAULT_BUILD_TIMEOUT_SECONDS
        )
        self.ack_wait_seconds = _positive_int(
            environ.get("SESSION_MEMORY_WATCH_ACK_WAIT_SECONDS"), self.build_timeout_seconds + 60
        )
        self.build_script = str(environ.get("SESSION_MEMORY_BUILD_SCRIPT") or DEFAULT_BUILD_SCRIPT)
        self.heartbeat_path = str(environ.get("SESSION_MEMORY_WATCH_HEARTBEAT") or DEFAULT_HEARTBEAT_PATH)
        self.health_max_age_seconds = _positive_int(
            environ.get("SESSION_MEMORY_WATCH_HEALTH_MAX_AGE_SECONDS"),
            self.fetch_wait_seconds + self.build_timeout_seconds + 60,
        )


# --- heartbeat -------------------------------------------------------------------


def _read_heartbeat(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_heartbeat(path: str, **fields: object) -> dict:
    payload = {
        **_read_heartbeat(path),
        **fields,
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "updated_at": _utc_now_iso(),
    }
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError:
        pass  # observability only; never fail the lane over it
    return payload


def _heartbeat_age_seconds(heartbeat: dict, *, now: datetime.datetime | None = None) -> float | None:
    raw = str(heartbeat.get("updated_at") or "")
    if not raw:
        return None
    try:
        updated = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=datetime.timezone.utc)
    current = now or datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (current - updated).total_seconds())


# --- build cycle -------------------------------------------------------------------


def parse_build_report(output: str) -> dict:
    """Last JSON object line of build-once.sh output (same rule as its guard)."""
    lines = [line for line in str(output or "").splitlines() if line.strip().startswith("{")]
    if not lines:
        return {}
    try:
        report = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {}
    return report if isinstance(report, dict) else {}


def run_build_script(
    session_ids: tuple[str, ...],
    *,
    settings: WatchSettings,
    environ: Mapping[str, str],
) -> tuple[int, dict]:
    env = dict(environ)
    if session_ids:
        env["SESSION_MEMORY_BUILD_SESSION_IDS"] = ",".join(session_ids)
    else:
        env.pop("SESSION_MEMORY_BUILD_SESSION_IDS", None)
    try:
        completed = subprocess.run(
            [settings.build_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.build_timeout_seconds + 60,
        )
    except subprocess.TimeoutExpired:
        return 124, {}
    except OSError:
        return 127, {}
    if completed.stdout:
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()
    return int(completed.returncode), parse_build_report(completed.stdout)


def session_outcomes(report: dict) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    for row in report.get("sessions") or []:
        if isinstance(row, dict) and row.get("session_id_hash"):
            outcomes[str(row["session_id_hash"])] = row
    return outcomes


def classify_message(
    event: SessionChangedEvent,
    *,
    rc: int,
    outcomes: dict[str, dict],
) -> tuple[str, str]:
    """Return (decision, error_class) with decision in {ack, nak}."""
    row = outcomes.get(event.session_id_hash)
    if row is None:
        # The build did not report this session (crash, timeout, env error): retry.
        return "nak", f"build_rc_{rc}" if rc != 0 else "missing_session_report"
    status = str(row.get("status") or "")
    if status in ACK_STATUSES:
        return "ack", ""
    return "nak", str(row.get("error_class") or "projection_failed")


def nak_delay_seconds(num_delivered: int, backoff: tuple[int, ...]) -> int:
    index = max(0, int(num_delivered) - 1)
    return backoff[min(index, len(backoff) - 1)]


def _num_delivered(msg) -> int:
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1


# --- consumer loop ---------------------------------------------------------------------


async def _fetch(sub, count: int, timeout: float) -> list:
    try:
        return list(await sub.fetch(max(1, count), timeout=max(0.001, timeout)))
    except asyncio.TimeoutError:
        return []
    except Exception as exc:  # nats TimeoutError and friends share this shape
        if type(exc).__name__ in {"TimeoutError", "FetchTimeoutError"}:
            return []
        raise


async def run_consume_loop(
    *,
    settings: WatchSettings,
    environ: Mapping[str, str],
    run_build: Callable[[tuple[str, ...]], tuple[int, dict]] | None = None,
    connect=None,
    log: Callable[[str], None] = print,
    max_cycles: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Durable pull-consume loop. Returns counters when ``max_cycles`` is reached."""
    if connect is None:
        import nats

        connect = nats.connect
    if run_build is None:
        run_build = lambda ids: run_build_script(ids, settings=settings, environ=environ)  # noqa: E731

    counters = {"cycles": 0, "event_cycles": 0, "sweeps": 0, "acked": 0, "naked": 0,
                "dead_lettered": 0, "terminated_invalid": 0, "consecutive_failures": 0}

    def heartbeat(**fields) -> None:
        _write_heartbeat(settings.heartbeat_path, **{**counters, **fields})

    def cycles_done() -> bool:
        return max_cycles is not None and counters["cycles"] >= max_cycles

    async def run_cycle(session_ids: tuple[str, ...], reason: str) -> tuple[int, dict]:
        heartbeat(phase="building", last_reason=reason, batch_size=len(session_ids))
        rc, report = await asyncio.to_thread(run_build, session_ids)
        counters["cycles"] += 1
        if rc == 0:
            counters["consecutive_failures"] = 0
        else:
            counters["consecutive_failures"] += 1
        heartbeat(phase="build_done", last_reason=reason, last_build_rc=rc, last_build_at=_utc_now_iso())
        log(f"[watch] cycle reason={reason} sessions={len(session_ids)} rc={rc}")
        return rc, report

    nc = await connect(settings.nats_url)
    try:
        js = nc.jetstream()
        await ensure_session_memory_stream(js, stream=settings.stream)
        from nats.js import api

        config = api.ConsumerConfig(
            durable_name=settings.durable,
            ack_policy=api.AckPolicy.EXPLICIT,
            ack_wait=float(settings.ack_wait_seconds),
            max_deliver=settings.max_deliver,
            max_ack_pending=settings.build_limit,
            filter_subject=settings.subject,
        )
        sub = await js.pull_subscribe(settings.subject, durable=settings.durable, stream=settings.stream, config=config)

        last_build = monotonic()
        if settings.startup_sweep and not cycles_done():
            counters["sweeps"] += 1
            await run_cycle((), "startup_sweep")
            last_build = monotonic()

        while not cycles_done():
            heartbeat(phase="waiting")
            wait = float(settings.fetch_wait_seconds)
            if settings.idle_sweep_seconds > 0:
                remaining = settings.idle_sweep_seconds - (monotonic() - last_build)
                if remaining <= 0:
                    counters["sweeps"] += 1
                    await run_cycle((), "idle_sweep")
                    last_build = monotonic()
                    continue
                wait = min(wait, remaining)
            batch = await _fetch(sub, settings.build_limit, wait)
            if not batch:
                continue

            # Debounce: a session arrives as several ingress deliveries; keep
            # extending while more events come, bounded by debounce_max.
            debounce_started = monotonic()
            while len(batch) < settings.build_limit and settings.debounce_seconds > 0:
                window = min(
                    float(settings.debounce_seconds),
                    float(settings.debounce_max_seconds) - (monotonic() - debounce_started),
                )
                if window <= 0:
                    break
                more = await _fetch(sub, settings.build_limit - len(batch), window)
                if not more:
                    break
                batch.extend(more)

            parsed: list[tuple[object, SessionChangedEvent]] = []
            for msg in batch:
                event = parse_session_changed_event(msg.data)
                if event is None:
                    counters["terminated_invalid"] += 1
                    await _dead_letter(js, settings, None, msg, error_class="invalid_event", log=log)
                    continue
                parsed.append((msg, event))
            if not parsed:
                continue
            session_ids = tuple(dict.fromkeys(event.session_id_hash for _, event in parsed))
            counters["event_cycles"] += 1
            rc, report = await run_cycle(session_ids, "changes")
            last_build = monotonic()
            outcomes = session_outcomes(report)
            for msg, event in parsed:
                decision, error_class = classify_message(event, rc=rc, outcomes=outcomes)
                if decision == "ack":
                    await msg.ack()
                    counters["acked"] += 1
                    continue
                attempts = _num_delivered(msg)
                if attempts >= settings.max_deliver:
                    if await _dead_letter(js, settings, event, msg, error_class=error_class, log=log):
                        counters["dead_lettered"] += 1
                    else:
                        counters["naked"] += 1
                    continue
                await msg.nak(delay=nak_delay_seconds(attempts, settings.nak_backoff_seconds))
                counters["naked"] += 1
                log(f"[watch] nak attempt={attempts} error={error_class}")
            heartbeat(phase="batch_done")
    finally:
        try:
            await nc.drain()
        except Exception:
            pass
    heartbeat(phase="stopped")
    return dict(counters)


async def _dead_letter(js, settings: WatchSettings, event, msg, *, error_class: str, log) -> bool:
    """Copy the event to the dead-letter subject, then terminate the original.

    Mirrors the ingress quarantine rule: durable terminal evidence must exist
    before the message leaves the consumer. If recording fails the message is
    nak'd (60s) and preserved for a later attempt.
    """
    attempts = _num_delivered(msg)
    payload = build_dead_letter_payload(event, attempts=attempts, error_class=error_class, raw_len=len(msg.data or b""))
    try:
        await js.publish(settings.dead_letter_subject, payload, stream=settings.stream)
    except Exception as exc:  # noqa: BLE001
        await msg.nak(delay=60)
        log(f"[watch] dead_letter_record_failed error={type(exc).__name__}")
        return False
    term = getattr(msg, "term", None)
    if callable(term):
        await term()
    else:
        await msg.ack()
    log(f"[watch] dead_letter attempts={attempts} error={error_class}")
    return True


# --- commands -----------------------------------------------------------------------------


def _cmd_consume(settings: WatchSettings, environ: Mapping[str, str], *, max_cycles: int | None) -> int:
    if not os.path.exists(settings.build_script):
        print(json.dumps({"schema_version": WATCH_CLI_SCHEMA_VERSION, "error": "env_missing",
                          "reason": "SESSION_MEMORY_BUILD_SCRIPT does not exist"}, sort_keys=True))
        return 2
    try:
        counters = asyncio.run(run_consume_loop(settings=settings, environ=environ, max_cycles=max_cycles))
    except Exception as exc:  # noqa: BLE001 - never print the URL or credentials
        _write_heartbeat(settings.heartbeat_path, phase="consume_failed", last_error=type(exc).__name__)
        print(json.dumps({"schema_version": WATCH_CLI_SCHEMA_VERSION, "error": "consume_failed",
                          "reason": type(exc).__name__}, sort_keys=True))
        return 1
    print(json.dumps({"schema_version": WATCH_CLI_SCHEMA_VERSION, **counters}, sort_keys=True))
    return 0


def _cmd_health(settings: WatchSettings, environ: Mapping[str, str]) -> int:
    run_mode = str(environ.get("RUN_MODE") or "cron").strip().lower()
    if run_mode != "watch":
        print(json.dumps({"schema_version": WATCH_CLI_SCHEMA_VERSION, "status": "not_applicable",
                          "run_mode": run_mode}, sort_keys=True))
        return 0
    heartbeat = _read_heartbeat(settings.heartbeat_path)
    age = _heartbeat_age_seconds(heartbeat)
    healthy = age is not None and age <= settings.health_max_age_seconds
    print(json.dumps({
        "schema_version": WATCH_CLI_SCHEMA_VERSION,
        "status": "ok" if healthy else "stale",
        "heartbeat_age_seconds": None if age is None else round(age, 1),
        "max_age_seconds": settings.health_max_age_seconds,
        "phase": str(heartbeat.get("phase") or ""),
        "consecutive_failures": _non_negative_int(heartbeat.get("consecutive_failures"), 0),
        "dead_lettered": _non_negative_int(heartbeat.get("dead_lettered"), 0),
    }, sort_keys=True))
    return 0 if healthy else 1


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-session-memory-watch",
        description="JetStream-driven trigger for the CouchDB->session-memory build lane.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--consume", action="store_true",
                       help="Run the durable session_memory.changed consumer (RUN_MODE=watch).")
    group.add_argument("--health", action="store_true",
                       help="Exit 0 when the heartbeat is fresh (RUN_MODE=watch), 1 when stale.")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="Stop after this many build cycles (verification only).")
    args = parser.parse_args(argv)

    env = os.environ if environ is None else environ
    settings = WatchSettings(env)
    if args.health:
        return _cmd_health(settings, env)
    return _cmd_consume(settings, env, max_cycles=args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
