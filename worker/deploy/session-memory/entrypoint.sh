#!/bin/bash
# session-memory worker 컨테이너 엔트리포인트.
# RUN_MODE: cron(기본, 경량 bash 스케줄러) | build | gc | backfill (one-off 검증용).
# 호스트 cron 스케줄과 동일: build=SESSION_MEMORY_BUILD_INTERVAL_SECONDS(기본 180초),
# gc=04:30 UTC, backfill=02:15 UTC.
# (외부 cron 바이너리 의존 제거 — 스크립트/python fork-exec만 사용.)
set -u

run_build()    { /app/deploy/build-once.sh; }
run_gc()       { python /app/deploy/gc-run.py; }
run_backfill() { python /app/deploy/backfill.py; }

retired_index_bridge_configured() {
  [ -n "${RETIRED_INDEX_BRIDGE_API_KEY:-}" ]
}

run_scheduled_gc() {
  if ! retired_index_bridge_configured; then
    echo "[scheduler] gc skipped: RETIRED_INDEX_BRIDGE_API_KEY not set"
    return 0
  fi
  run_gc
}

run_scheduled_backfill() {
  if ! retired_index_bridge_configured; then
    echo "[scheduler] backfill skipped: RETIRED_INDEX_BRIDGE_API_KEY not set"
    return 0
  fi
  run_backfill
}

positive_int_or_default() {
  local value="${1:-}"
  local default_value="$2"
  if [[ "$value" =~ ^[0-9]+$ ]] && [ "$((10#$value))" -gt 0 ]; then
    printf '%s\n' "$((10#$value))"
  else
    printf '%s\n' "$default_value"
  fi
}

read_day_stamp() {
  local path="$1"
  local value=""
  if [ -r "$path" ]; then
    read -r value < "$path" || value=""
  fi
  if [[ "$value" =~ ^[0-9]{8}$ ]]; then
    printf '%s\n' "$value"
  fi
}

write_day_stamp() {
  local path="$1"
  local value="$2"
  printf '%s\n' "$value" > "$path" || echo "[scheduler] stamp write failed: $path"
}

main() {
  cd /app || exit 1
  mkdir -p state

  case "${RUN_MODE:-cron}" in
    build)    exec /app/deploy/build-once.sh ;;
    gc)       exec python /app/deploy/gc-run.py ;;
    backfill) exec python /app/deploy/backfill.py ;;
    cron)
      build_interval_seconds=$(positive_int_or_default "${SESSION_MEMORY_BUILD_INTERVAL_SECONDS:-}" 180)
      scheduler_sleep_seconds=$(positive_int_or_default "${SESSION_MEMORY_SCHEDULER_SLEEP_SECONDS:-}" 60)
      echo "[entrypoint] scheduler started (build=${build_interval_seconds}s, sleep=${scheduler_sleep_seconds}s, gc=04:30, backfill=02:15 UTC)"
      last_bf_stamp="state/session-memory-backfill-last-day"
      last_gc_stamp="state/session-memory-gc-last-day"
      last_build=0
      last_bf=$(read_day_stamp "$last_bf_stamp")
      last_gc=$(read_day_stamp "$last_gc_stamp")
      while true; do
        read -r now hour minute day <<< "$(date -u "+%s %H %M %Y%m%d")"
        minute_of_day=$((10#$hour * 60 + 10#$minute))
        if [ $((now - last_build)) -ge "$build_interval_seconds" ]; then
          run_build || echo "[scheduler] build rc=$?"
          last_build="$now"
        fi
        if [ "$minute_of_day" -ge $((2 * 60 + 15)) ] && [ "$last_bf" != "$day" ]; then
          if run_scheduled_backfill; then
            last_bf="$day"
            write_day_stamp "$last_bf_stamp" "$day"
          else
            echo "[scheduler] backfill rc=$?"
          fi
        fi
        if [ "$minute_of_day" -ge $((4 * 60 + 30)) ] && [ "$last_gc" != "$day" ]; then
          if run_scheduled_gc; then
            last_gc="$day"
            write_day_stamp "$last_gc_stamp" "$day"
          else
            echo "[scheduler] gc rc=$?"
          fi
        fi
        sleep "$scheduler_sleep_seconds"
      done
      ;;
    *) echo "unknown RUN_MODE=${RUN_MODE}" >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
