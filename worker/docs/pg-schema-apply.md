# PostgreSQL schema apply (`pg-schema-apply`)

`neuron-knowledge pg-schema-apply`는 `worker/lib/agent_knowledge/postgres_store/pgvector_schema.sql`을
대상 PostgreSQL에 적용하고, 적용 후 실제 객체 존재를 다시 검증한다. 이 명령은
LBrain authority 스키마를 배포 절차에서 명시적으로 적용하기 위한 경로다.
앱 시작 시 자동 실행되지 않는다.

## 안전 계약

- 기본 실행은 **plan-only**다. DSN을 읽지도, 연결하지도 않는다.
- `--check`는 **read-only**다. 테이블·인덱스·확장·컬럼 존재만 확인한다.
- `--execute`는 체크인된 DDL 전체를 한 번에 적용한다. DDL은 모두
  `IF NOT EXISTS`/additive 문장이며 `DROP`을 포함하지 않는다.
- `--target local_test` 실행은 DSN host가 `localhost`, `127.0.0.1`, `::1`
  또는 unix socket이고, `dbname`/`user`가 명시된 경우에만 허용된다.
- `--target production --execute`는 아래 네 가지를 모두 요구한다.
  - `--approved`
  - `--approval-ref` = plan 출력의 `schema_sha256` (승인 대상 DDL과 정확히 결속)
  - `--backup-ref`
  - `--rollback-ref`
- 출력은 DSN, host, credential을 포함하지 않는다. 예외도 type name만 남긴다.

## 실행

```bash
# 1) plan: 적용 대상 DDL과 schema hash 확인 (DB 연결 없음)
neuron-knowledge pg-schema-apply

# 2) read-only 존재 확인
neuron-knowledge pg-schema-apply --check

# 3) disposable/local 테스트 DB에 적용
NEURON_LBRAIN_PGVECTOR_DSN=postgresql://...@127.0.0.1:5432/... \
  neuron-knowledge pg-schema-apply --execute

# 4) production 적용 (승인 기록에 schema hash를 남긴 뒤)
neuron-knowledge pg-schema-apply \
  --target production \
  --execute \
  --approved \
  --approval-ref <plan 출력의 schema_sha256 (이미 sha256: prefix 포함)> \
  --backup-ref <backup-id> \
  --rollback-ref <rollback-runbook-or-snapshot-id>
```

### production 실행 전 체크리스트

1. `--target production --check`로 현재 객체 상태를 저장한다.
2. plan 출력의 `schema_sha256`을 변경 승인 기록에 남긴다.
3. 대상 DB의 `dbname`/`user`와 backup ref, rollback runbook/snapshot ref를 확인한다.
4. `--execute`는 단일 deploy Job/step에서만 실행한다. replica startup hook에 넣지 않는다.
5. 실행 후 같은 명령에 `--check`를 붙여 `missing`이 비어 있는지 확인한다.
6. `missing`이 비어 있지 않거나, extension이 `public` 밖에 있거나, DDL 오류가 나면
   중단하고 rollback ref의 절차를 따른다.

DSN은 다음 순서로 환경변수에서 읽는다.

1. `NEURON_LBRAIN_PGVECTOR_DSN`
2. `LLM_BRAIN_PGVECTOR_DSN`
3. `NEURON_LEDGER_PG_DSN`

`service=` DSN은 허용하지 않는다. `dbname`과 `user`가 명시되어야 한다.

## 출력

주요 필드:

- `status`: `planned`, `ok`, `missing`, `applied`, `verification_failed`,
  `denied`, `configuration_required`, `configuration_rejected`, `failed`
- `schema_sha256`: 승인에 사용할 DDL hash
- `tables`, `indexes`, `extensions`, `columns`: DDL이 선언한 객체
- `missing`: `denied`에서는 미충족 승인 요건, `missing`/`verification_failed`에서는
  없거나 다른 schema에 있는 객체
- `mutation_performed`, `production_mutation_performed`, `network_used`
- `approval_ref_present`, `backup_ref_present`, `rollback_ref_present`

## 배포 연결

이 명령은 one-shot deploy Job/step에서 호출하는 것을 전제로 한다.
replica마다 앱 시작 시 실행하면 동시 DDL 경쟁이 생기므로, 서비스 startup hook이
아니라 배포 절차의 단일 실행 지점에 연결한다. 실제 k3s/Argo manifest는
private ops repo가 소유한다.
