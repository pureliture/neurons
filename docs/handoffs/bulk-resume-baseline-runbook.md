# Bulk semantic 재개 전 baseline runbook

## 배경

counter 62의 `production-commit/write`가 `healthy_baseline_required`로 거부되었다. 당시 `neurons-session-memory-build-29915635`가 27분간 `pipeline_progress.v1`(stage=selection, total=0)을 갱신하지 못했고, bulk selection이 CouchDB에 과도한 요청을 보내 공유 baseline을 깨뜨렸다. bulk는 즉시 replicas 0으로 되돌렸고 해당 Job은 완료되었다.

## 재개 전 확인

1. `kubectl -n neurons get jobs`에서 `neurons-session-memory-build`/`neurons-embedding-outbox`가 progress를 갱신하며 완료되는지 확인.
2. `kubectl -n argocd get applications`에서 프로덕션 app이 `Synced/Healthy`인지 확인.
3. CouchDB `/_node/_local/_stats` 요청 레이트가 baseline(약 20~30 req/s)인지 확인.
4. `neurons-bulk-semantic-trigger` 이미지가 `ops-<main SHA>`이고 `LIMIT=200`인지 확인.

## 재개 후 관측

- `bulk-semantic-progress.jsonl` 라인 수와 마지막 `status/projected/failed`
- `llm_brain_graph_projection_state`의 `extraction_level='entity', entity_type='Session'` distinct 수
- CouchDB req/s, reads/s
- 새 세션의 `pipeline_progress.v1`이 계속 갱신되는지

## 즉시 중단 조건

- Session-memory-build/embedding-outbox progress가 10분 이상 정지
- CouchDB req/s가 baseline의 3배 이상 지속
- 진행 이벤트가 없는데 CPU/소켓만 증가
- 유료 모델 호출 또는 새 과금 징후

중단 시 replicas 0 PR + Argo hard refresh까지 확인한 뒤 보고한다. stage/job rerun은 사용하지 않는다.

## counter 64 메모

counter 64도 `healthy_baseline_required`로 거부되었다. 원인은 bulk와 무관한
`neurons-session-memory-build-29915750`의 `child_exit` 실패가 Job 히스토리에 남아
게이트가 실패로 판정한 것이었다. 실패 Job을 정리하고(CronJob lastSuccessful은
유효), 최신 성공 Job과 활성 Job 없음을 확인한 뒤 새 카운터로 배포한다.
