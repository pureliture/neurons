# PostgreSQL 기억 임베딩 처리기

`neuron-knowledge pg-embedding-outbox`는 승인 경로가 등록한 임베딩 대기 작업을 기존 lease/CAS 처리기로 소비한다. 카드 생성·사람 승인·거부·교체는 수행하지 않는다. 그래프 대기 작업은 별도 `pg-graph-project` 처리기의 책임이다.

## 기본 상태

- 기본 실행은 활성화 요청을 요구하며 DB/provider를 만들지 않는다.
- `--dry-run`은 설정 계획만 반환한다. 대상 대기열 조회·선택·검증 결과가 아니다.
- 실행하려면 운영자가 모델 사용과 대기열 변경을 승인한 뒤 `--execute --allow-model-calls`를 명시해야 한다. CLI 플래그 자체는 사람의 승인 기록을 대신하지 않는다.
- DB 연결과 provider 자격증명은 기존 환경 변수/비밀 저장소로 주입한다. argv나 출력에 자격 값을 넣지 않는다.

## 실행 예

```bash
# 모델/DB 연결 없이 계획 확인
neuron-knowledge pg-embedding-outbox --dry-run

# 승인된 환경에서 한 번에 최대 한 작업 처리
neuron-knowledge pg-embedding-outbox --execute --allow-model-calls --once --max-jobs 1

# 승인된 환경에서 유한 반복 처리. 무제한 daemon이 아니다.
neuron-knowledge pg-embedding-outbox --execute --allow-model-calls --batch-size 1 --max-jobs 20 --max-iterations 60
```

정식 모델·차원·프로필은 `gemini-embedding-2 / 3072 / lbrain-memory-gemini-embedding-2-v1`이다. 다른 프로필이나 provider 주소가 없는 설정은 임대/모델 호출 전에 거부한다. 테스트용 벡터 함수를 운영 기본값으로 쓰지 않는다.

## 상한·실패·종료

- `--max-jobs`는 **한 프로세스의 처리 시도 상한**이며 일일/금액 한도가 아니다. SDK 재시도는 최대 2회(요청은 최초 포함 최대 3회)로 명시하지만, 통화 비용 상한으로 설명하면 안 된다. 상시 스케줄은 별도 비용·활성화 승인을 요구한다.
- 모델 timeout은 검증에 사용한 환경과 실제 SDK에 같은 값으로 전달한다. 외부 프로세스의 다른 timeout 설정이 주입된 실행 환경을 덮어쓰지 않는다.
- 여러 작업을 함께 임대하면 순서대로 처리하므로 최소 lease는 `(timeout × 3 + 15초) × min(batch-size, max-jobs)`이다. 부족하면 DB/provider 생성 전 거부한다. 기본 batch 1은 유지하며 batch 2/timeout 30초에는 lease 최소 210초가 필요하다. 이 검사는 설정 기반 최소 여유이며 네트워크/DB 지연의 무제한 연장을 보장하지 않는다.
- 출력의 `attempted`, `completed`, `failed`, `stale`을 구분한다. 처리 시도 수를 임베딩 성공 수로 계산하지 않는다. 중간 DB 오류로 batch가 종료돼도 그전에 확인된 처리 수는 보존하며, 확인되지 않은 저장을 완료로 세지 않는다. `model_calls`는 provider 함수 호출 수이며 SDK 내부 HTTP 재시도 수가 아니다.
- provider 실패는 기존 재시도/lease 규칙으로 기록하며 실패가 있으면 비정상 종료한다. 처리 오류/DSN/입력 원문은 출력하지 않고 고정된 오류 종류만 기록한다.
- credential 형태 입력은 provider 호출 전에 거부한다. 입력은 해시 대응을 깨뜨리지 않도록 자동 재작성하지 않는다.
- SIGTERM은 다음 batch 시작을 막고 진행 중 batch가 끝난 뒤 종료한다. 최대 provider timeout과 lease 관계를 검증한다. 재시작은 기존 임대·CAS 상태를 사용한다.
- 롤백은 처리기를 정지하고 이전 image/명령으로 복원한다. 승인·감사·대기열·기존 실패 이력은 삭제하지 않는다.

## 검증 경계

`tests/test_embedding_outbox_runtime.py`는 기본 비활성, 프로필 거부, 비밀 외부 전달 거부, 시도 상한, 실패 집계와 router 연결을 검사한다. `LBRAIN_TEST_PG_DSN`을 **격리된 로컬 테스트 DB**로 제공하면 실제 PostgreSQL 저장과 두 번째 실행의 중복 임베딩 없음도 검사한다. 이 테스트의 주입 embedder는 test-only이며 실제 provider 품질·운영 적용·새 세션 기억 복원을 증명하지 않는다.
