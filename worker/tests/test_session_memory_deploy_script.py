import subprocess
from pathlib import Path


def test_build_once_uses_couchdb_native_builder_with_qdrant_default() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert "couchdb-session-memory-build" in script
    assert "agent_knowledge.session_memory.neuron_session_memory" not in script
    assert "SESSION_MEMORY_PROJECTION_BACKEND" in script
    assert "QDRANT_URL" in script
    assert "neurons_mirror_gemini_3072_v1" in script
    assert "RETIRED_INDEX_BRIDGE_API_KEY" not in script


def test_build_once_writes_matching_live_approval_for_couchdb_builder() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert '"operation": "couchdb_session_memory_build"' in script
    assert '"redaction_required": True' in script
    assert '"command": {"argv": argv}' in script
    assert "state/couchdb-build-approval.json" in script


def test_build_once_enforces_process_timeout() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert "SESSION_MEMORY_BUILD_TIMEOUT_SECONDS" in script
    assert "timeout \"${SESSION_MEMORY_BUILD_TIMEOUT_SECONDS:-300}\"" in script


def test_session_memory_image_installs_qdrant_projection_dependencies() -> None:
    dockerfile = Path("Dockerfile.session-memory").read_text(encoding="utf-8")

    assert "qdrant-client>=1.10" in dockerfile
    assert "openai>=1.0" in dockerfile


def test_entrypoint_build_interval_is_env_configurable() -> None:
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert "SESSION_MEMORY_BUILD_INTERVAL_SECONDS" in script
    assert "SESSION_MEMORY_SCHEDULER_SLEEP_SECONDS" in script
    assert "positive_int_or_default" in script
    assert '"${SESSION_MEMORY_BUILD_INTERVAL_SECONDS:-}" 180' in script
    assert '"${SESSION_MEMORY_SCHEDULER_SLEEP_SECONDS:-}" 60' in script
    assert "10#$m % 3" not in script


def test_entrypoint_positive_int_helper_uses_base_10_values() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                [
                    "set -euo pipefail",
                    "source deploy/session-memory/entrypoint.sh",
                    "positive_int_or_default 010 60",
                    "positive_int_or_default 0 60",
                    "positive_int_or_default abc 60",
                    "positive_int_or_default -5 60",
                ]
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["10", "60", "60", "60"]


def test_entrypoint_has_no_retired_bridge_daily_lanes() -> None:
    """The retired external index bridge gc/backfill lanes are gone.

    Those lanes auto-approved live hard-deletes against a retired backend, so
    they must not come back with the scheduler.
    """
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert "run_scheduled_gc" not in script
    assert "run_scheduled_backfill" not in script
    assert "gc-run.py" not in script
    assert "backfill.py" not in script
    assert "RETIRED_INDEX_BRIDGE" not in script


def test_entrypoint_daily_job_stamps_survive_scheduler_restart(tmp_path: Path) -> None:
    stamp_path = tmp_path / "last-day"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                [
                    "set -euo pipefail",
                    "source deploy/session-memory/entrypoint.sh",
                    f"stamp={stamp_path}",
                    "write_day_stamp \"$stamp\" 20260705",
                    "read_day_stamp \"$stamp\"",
                    "printf '%s\\n' bad-value > \"$stamp\"",
                    "read_day_stamp \"$stamp\"",
                ]
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["20260705"]


def test_entrypoint_scheduler_runs_only_the_build_lane() -> None:
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert "cd /app || exit 1" in script
    assert "[entrypoint] scheduler started" in script
    assert "run_build() { /app/deploy/build-once.sh; }" in script
    assert 'if [ $((now - last_build)) -ge "$build_interval_seconds" ]; then' in script
    assert "run_build || echo" in script


def test_entrypoint_watch_mode_is_opt_in_and_wraps_jetstream_consumer() -> None:
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert 'case "${RUN_MODE:-cron}" in' in script
    assert "watch) run_watch_loop ;;" in script
    assert "couchdb-session-memory-watch" in script
    assert "run_interruptible watch_cli --consume" in script
    assert "SESSION_MEMORY_WATCH_FAILURE_BACKOFF_MAX_SECONDS" in script
    assert "trap stop_watch TERM INT" in script
    assert "_changes" not in script


def test_entrypoint_watch_failure_backoff_is_bounded() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                [
                    "set -euo pipefail",
                    "source deploy/session-memory/entrypoint.sh",
                    "failure_backoff_seconds 1 300",
                    "failure_backoff_seconds 2 300",
                    "failure_backoff_seconds 4 300",
                    "failure_backoff_seconds 7 300",
                    "failure_backoff_seconds 50 300",
                ]
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["5", "10", "40", "300", "300"]


def test_entrypoint_watch_loop_restarts_consumer_with_backoff(tmp_path: Path) -> None:
    log_path = tmp_path / "calls.log"
    script = "\n".join(
        [
            "set -euo pipefail",
            "source deploy/session-memory/entrypoint.sh",
            f"log={log_path}",
            "count=0",
            "watch_cli() {",
            '  count=$((count + 1)); echo "consume $*" >> "$log"',
            '  if [ "$count" -ge 3 ]; then kill -TERM "$$"; fi',
            '  [ "$count" -ne 2 ]',
            "}",
            'sleep() { echo "sleep $1" >> "$log"; }',
            "SESSION_MEMORY_WATCH_FAILURE_BACKOFF_MAX_SECONDS=300 run_watch_loop",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "consume --consume",
        "consume --consume",
        "sleep 5",
        "consume --consume",
    ]
    assert "[watch] consumer rc=1 consecutive_failures=1 backoff=5s" in result.stdout


def test_build_once_passes_watch_session_ids_as_explicit_selection() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert "SESSION_MEMORY_BUILD_SESSION_IDS" in script
    assert '"--session-id-hash" "$watch_session_id"' in script


def test_compose_defaults_to_cron_and_health_checks_watch_heartbeat() -> None:
    compose = Path("deploy/session-memory/compose.yaml").read_text(encoding="utf-8")

    assert "RUN_MODE: ${RUN_MODE:-cron}" in compose
    assert "RAG_INGRESS_NATS_URL: ${RAG_INGRESS_NATS_URL:-nats://127.0.0.1:4222}" in compose
    assert "SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS: ${SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS:-2}" in compose
    assert "SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS: ${SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS:-900}" in compose
    assert "SESSION_MEMORY_WATCH_MAX_DELIVER: ${SESSION_MEMORY_WATCH_MAX_DELIVER:-5}" in compose
    assert '"couchdb-session-memory-watch", "--health"' in compose
