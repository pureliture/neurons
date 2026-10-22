#!/bin/bash
# session-memory worker 컨테이너 엔트리포인트.
# RUN_MODE: cron(기본, 경량 bash 스케줄러) | build (one-off 검증용).
# 호스트 cron 스케줄과 동일: build=SESSION_MEMORY_BUILD_INTERVAL_SECONDS(기본 180초).
#
# retired external index bridge lane(gc/backfill)은 삭제됐다: 그 백엔드는 이미
# 퇴역했고 라이브에도 없으므로, 은퇴한 백엔드에 대해 자동 승인 live delete를
# 실행하는 lane은 유지할 수 없다. live GC는 operator 승인 절차로만 수행한다.
# (외부 cron 바이너리 의존 제거 — 스크립트/python fork-exec만 사용.)
set -u

run_build() { /app/deploy/build-once.sh; }

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
    build) exec /app/deploy/build-once.sh ;;
    cron)
      build_interval_seconds=$(positive_int_or_default "${SESSION_MEMORY_BUILD_INTERVAL_SECONDS:-}" 180)
      scheduler_sleep_seconds=$(positive_int_or_default "${SESSION_MEMORY_SCHEDULER_SLEEP_SECONDS:-}" 60)
      echo "[entrypoint] scheduler started (build=${build_interval_seconds}s, sleep=${scheduler_sleep_seconds}s)"
      last_build=0
      while true; do
        now=$(date -u "+%s")
        if [ $((now - last_build)) -ge "$build_interval_seconds" ]; then
          run_build || echo "[scheduler] build rc=$?"
          last_build="$now"
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
