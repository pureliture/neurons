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


def test_entrypoint_daily_jobs_use_due_window_not_exact_minute() -> None:
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert 'read -r now hour minute day <<< "$(date -u "+%s %H %M %Y%m%d")"' in script
    assert "minute_of_day=$((10#$hour * 60 + 10#$minute))" in script
    assert '[ "$hm" = "04:30" ]' not in script
    assert '[ "$hm" = "02:15" ]' not in script
    assert '[ "$minute_of_day" -ge $((4 * 60 + 30)) ]' in script
    assert '[ "$minute_of_day" -ge $((2 * 60 + 15)) ]' in script


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


def test_entrypoint_daily_scheduler_uses_persisted_stamps() -> None:
    script = Path("deploy/session-memory/entrypoint.sh").read_text(encoding="utf-8")

    assert "cd /app || exit 1" in script
    assert "[entrypoint] scheduler started" in script
    assert 'last_bf_stamp="state/session-memory-backfill-last-day"' in script
    assert 'last_gc_stamp="state/session-memory-gc-last-day"' in script
    assert 'last_bf=$(read_day_stamp "$last_bf_stamp")' in script
    assert 'last_gc=$(read_day_stamp "$last_gc_stamp")' in script
    assert "if run_scheduled_backfill; then" in script
    assert "if run_scheduled_gc; then" in script
    assert 'write_day_stamp "$last_bf_stamp" "$day"' in script
    assert 'write_day_stamp "$last_gc_stamp" "$day"' in script


def test_entrypoint_scheduled_retired_bridge_jobs_skip_without_token() -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                [
                    "set -euo pipefail",
                    "source deploy/session-memory/entrypoint.sh",
                    "unset RETIRED_INDEX_BRIDGE_API_KEY",
                    "run_scheduled_backfill",
                    "run_scheduled_gc",
                ]
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "backfill skipped: RETIRED_INDEX_BRIDGE_API_KEY not set" in result.stdout
    assert "gc skipped: RETIRED_INDEX_BRIDGE_API_KEY not set" in result.stdout
