#!/bin/bash
# session-memory worker 컨테이너 엔트리포인트.
# RUN_MODE: cron(기본, 경량 bash 스케줄러) | watch(NATS JetStream 이벤트 기반) |
#           build (one-off 검증용).
# cron: 호스트 cron 스케줄과 동일, build=SESSION_MEMORY_BUILD_INTERVAL_SECONDS(기본 180초).
# watch: `couchdb-session-memory-watch --consume`가 NATS JetStream
#        `session_memory.changed` durable consumer로 세션 변경 이벤트를 받아
#        같은 build-once.sh를 세션 지정 cycle로 실행한다(재시도/DLQ는 JetStream).
#        checkpoint·state 디렉터리는 cron과 공유하므로 RUN_MODE만 바꿔 rollback한다.
#
# retired external index bridge lane(gc/backfill)은 삭제됐다: 그 백엔드는 이미
# 퇴역했고 라이브에도 없으므로, 은퇴한 백엔드에 대해 자동 승인 live delete를
# 실행하는 lane은 유지할 수 없다. live GC는 operator 승인 절차로만 수행한다.
# (외부 cron 바이너리 의존 제거 — 스크립트/python fork-exec만 사용.)
set -u

run_build() { /app/deploy/build-once.sh; }
watch_cli() { python -m agent_knowledge.cli couchdb-session-memory-watch "$@"; }

# 연속 실패 n회째의 대기 시간: 5s * 2^(n-1), 상한 max.
failure_backoff_seconds() {
  local failures="$1"
  local max_seconds="$2"
  local seconds=5
  local i=1
  while [ "$i" -lt "$failures" ] && [ "$seconds" -lt "$max_seconds" ]; do
    seconds=$((seconds * 2))
    i=$((i + 1))
  done
  if [ "$seconds" -gt "$max_seconds" ]; then
    seconds="$max_seconds"
  fi
  printf '%s\n' "$seconds"
}

# 포그라운드 child는 trap을 막으므로 background+wait로 SIGTERM을 즉시 받는다.
run_interruptible() {
  "$@" &
  watch_child=$!
  wait "$watch_child"
  local rc=$?
  watch_child=""
  return "$rc"
}

stop_watch() {
  echo "[watch] stop requested"
  if [ -n "${watch_child:-}" ]; then
    kill "$watch_child" 2>/dev/null || true
  fi
  exit 0
}

# RUN_MODE=watch: JetStream durable consumer(Python 상주)를 감싸는 재기동 루프.
# consumer는 정상 동작 중 종료하지 않으며, NATS 오류(rc=1)나 설정 오류(rc=2)로
# 끝나면 bounded backoff 후 다시 띄운다. build 실행은 consumer 안에서 build-once.sh로 한다.
# 테스트 훅: 기본은 무한 루프. 테스트에서 재정의해 반복 횟수를 제한한다.
watch_should_continue() { return 0; }

run_watch_loop() {
  local backoff_max failures rc backoff
  backoff_max=$(positive_int_or_default "${SESSION_MEMORY_WATCH_FAILURE_BACKOFF_MAX_SECONDS:-}" 300)
  failures=0
  watch_child=""
  trap stop_watch TERM INT
  echo "[entrypoint] watch started (debounce=${SESSION_MEMORY_WATCH_DEBOUNCE_SECONDS:-2}s, idle_sweep=${SESSION_MEMORY_WATCH_IDLE_SWEEP_SECONDS:-900}s, max_deliver=${SESSION_MEMORY_WATCH_MAX_DELIVER:-5})"
  while watch_should_continue; do
    if run_interruptible watch_cli --consume; then
      failures=0
      continue
    fi
    rc=$?
    failures=$((failures + 1))
    backoff=$(failure_backoff_seconds "$failures" "$backoff_max")
    echo "[watch] consumer rc=${rc} consecutive_failures=${failures} backoff=${backoff}s"
    sleep "$backoff" &
    watch_child=$!
    wait "$watch_child" || true
    watch_child=""
  done
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
    watch) run_watch_loop ;;
    *) echo "unknown RUN_MODE=${RUN_MODE}" >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
