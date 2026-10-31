#!/bin/bash
# 전용 PG 컨테이너와 독립 테스트 제어 디렉터리가 명시된 경우만 실행한다.
set -euo pipefail
control=${LBRAIN_TEST_BACKUP_CONTROL_HOST_DIR:?작업 전용 제어 디렉터리 필요}
container=${LBRAIN_TEST_PG_CONTAINER:?작업 전용 PostgreSQL 컨테이너 필요}
[[ -d "$control" && "$control" = /* ]]
deadline=$((SECONDS+120))
trap 'touch "$control/failed"' ERR
while [[ ! -f "$control/request.json" ]]; do
  if (( SECONDS >= deadline )); then touch "$control/failed"; exit 1; fi
  sleep 1
done
schema=$(jq -er '.schema' "$control/request.json")
source_database=$(jq -er '.source_database' "$control/request.json")
target_database=$(jq -er '.target_database' "$control/request.json")
[[ "$schema" =~ ^c1_test_[a-f0-9]{32}$ ]]
[[ "$target_database" =~ ^lb_restore_[a-f0-9]{32}$ ]]
[[ "$source_database" == neurons_correction_goal_canonical ]]
archive="/tmp/${target_database}.dump"
cleanup() {
  docker exec "$container" rm -f "$archive" >/dev/null 2>&1 || true
}
trap cleanup EXIT
timeout 90s docker exec "$container" pg_dump -U postgres -Fc -n "$schema" -f "$archive" "$source_database"
timeout 90s docker exec "$container" pg_restore -U postgres --exit-on-error --no-owner --dbname "$target_database" "$archive"
touch "$control/restored"
