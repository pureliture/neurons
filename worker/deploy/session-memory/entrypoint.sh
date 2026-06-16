#!/bin/bash
# session-memory worker 컨테이너 엔트리포인트.
# RUN_MODE: cron(기본, 경량 bash 스케줄러) | build | gc | backfill (one-off 검증용).
# 호스트 cron 스케줄과 동일: build=*/3분, gc=04:30 UTC, backfill=02:15 UTC.
# (외부 cron 바이너리 의존 제거 — 스크립트/python fork-exec만 사용.)
set -u
cd /app
mkdir -p state

run_build()    { /app/deploy/build-once.sh; }
run_gc()       { python /app/deploy/gc-run.py; }
run_backfill() { python /app/deploy/backfill.py; }

case "${RUN_MODE:-cron}" in
  build)    exec /app/deploy/build-once.sh ;;
  gc)       exec python /app/deploy/gc-run.py ;;
  backfill) exec python /app/deploy/backfill.py ;;
  cron)
    echo "[entrypoint] scheduler 시작 (build=*/3m, gc=04:30, backfill=02:15 UTC)"
    last_gc=""; last_bf=""
    while true; do
      m=$(date -u +%M); hm=$(date -u +%H:%M); day=$(date -u +%Y%m%d)
      if [ $((10#$m % 3)) -eq 0 ]; then run_build || echo "[scheduler] build rc=$?"; fi
      if [ "$hm" = "04:30" ] && [ "$last_gc" != "$day" ]; then
        run_gc || echo "[scheduler] gc rc=$?"; last_gc="$day"
      fi
      if [ "$hm" = "02:15" ] && [ "$last_bf" != "$day" ]; then
        run_backfill || echo "[scheduler] backfill rc=$?"; last_bf="$day"
      fi
      sleep 60
    done
    ;;
  *) echo "unknown RUN_MODE=${RUN_MODE}" >&2; exit 2 ;;
esac
