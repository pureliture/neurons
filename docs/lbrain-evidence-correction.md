# LBrain 근거 기반 사실 교정

`brain_object_correct`는 작업 중 확인한 명시적 source와 accepted/current MemoryCard의 값이 다를 때 successor를 만든다. 서버의 source/field 정책을 통과한 교정에는 건별 사람 승인이 필요하지 않다.

현재 구현은 초기 adapter에 한정하며 전체 요구의 완료 상태가 아니다. 지연 세션 수집과의 합류 및 재현된 summary/payload 불일치 등은 [설계 감사](validation/2026-10-25-lbrain-correction-reconciliation-audit.md)에 기록했다. 이 문제를 해결하기 전 전체 기능이 준비됐다고 판단하지 않는다.

자동 재검토의 PG 영속 예약·lease 회수·판단 cache·독립 감시 코어와 실험용 연결기를 추가했다. 별도 Ubuntu 프로세스에서 장애 복구와 구조화된 지연 근거의 자동 교정을 [실증](validation/2026-10-25-lbrain-recheck-runtime.md)했다. 운영 수집 연결·범용 모델 판단·충돌 B 읽기 정책은 아직 완료하지 않았다.

## 지원 범위

초기 `registered_json` adapter는 source registry에서 정책에 지정된 JSON 필드를 직접 읽는다. 정책에는 project/repository, 원본 카드 계보, 대상 field, 허용 source의 device/root/path hash, JSON pointer, 값 대응표, 표시 template과 최대 관측 나이가 들어간다. 요청자가 새 값이나 임의 필드·source locator를 지정할 수 없다.

source는 전체 redacted bytes의 SHA-256이 등록된 `content_hash`와 같아야 한다. `source_ref_from_catalog_event`의 기본 `src_<hash>` 식별자로 content-addressed revision을 사용하며, 경로별 최신 관측 revision·`full_sync`·project 접근 정책을 확인한다. 여러 source를 정책에 등록하면 모두 제출하고 같은 값이 도출되어야 한다. 실제 외부 source가 관측 후 변경되었는지는 registry만으로 증명할 수 없으므로 freshness window를 짧고 명시적으로 설정한다.

현재는 status/evidence의 명시적 사실과 task의 상태 필드를 지원한다. preference, intent, 평가, 자유로운 code 의미 해석, test 결과의 범위를 넘어선 runtime/deployment 주장은 자동 교정하지 않는다. code·Git·runtime adapter는 별도 source 검증 계약을 갖춰야 추가할 수 있다.

## 정책 예시

원천 데이터가 `{"facts":{"status":"ready"}}`인 경우의 설정이다. 아래 hash와 원본 `memory_id`는 운영자가 검증한 실제 값으로 채운다. 정책은 비공개 runtime 설정에 둔다.

```json
{
  "schema_version": "evidence_correction_policy.v1",
  "rules": [{
    "policy_id": "feature-status",
    "project": "synthetic-correction",
    "repository": "neurons",
    "memory_id": "original-memory-id",
    "field_path": "typed_payload.status_value",
    "summary_template": "기능 상태: {value}",
    "max_age_seconds": 300,
    "value_map": {},
    "sources": [{
      "adapter": "registered_json",
      "root_id": "facts-root",
      "device_id_hash": "sha256:<device-digest>",
      "relative_path_hash": "sha256:<relative-path-digest>",
      "json_pointer": "/facts/status"
    }]
  }]
}
```

`value_map`이 비어 있으면 source 값은 문자열이어야 한다. boolean 같은 값을 상태 문자열로 변환하려면 `{"true":"ready","false":"missing"}`처럼 canonical JSON 값에 대응하는 표를 설정한다. source text 자체가 target과 value를 주장하는 문장인 것만으로는 정책 대응이 성립하지 않는다.

## 활성화와 호출

공통 service 플래그는 `--allow-evidence-backed-object-corrections --evidence-correction-policy-file <file>`이다. 기본값은 비활성이다. PostgreSQL schema를 먼저 적용하고, source registry와 canonical steward store를 연결한다.

HTTP는 `--surface correction`과 전용 `LLM_BRAIN_CORRECTION_TOKEN`을 사용한다. 최소 32자의 bearer가 필요하며 tool 목록은 `brain.resolve`, `brain_object_correct` 두 개다. 기존 public agent 및 admin surface로는 교정할 수 없다. stdio는 위 feature flag와 유효한 정책이 함께 설정된 프로세스에서 도구를 노출한다. 토큰 값은 LLM 입력이나 receipt에 포함하지 않는다.

LLM은 `brain.resolve(mode=list)`에서 얻은 `id`와 `content_hash`, 작업에서 확인한 source revision, integration이 제공한 `policy_id`를 사용한다.

```json
{
  "project": "synthetic-correction",
  "repository": "neurons",
  "target_memory_id": "memory-id-from-brain-resolve",
  "expected_content_hash": "sha256:<queried-card-digest>",
  "policy_id": "feature-status",
  "idempotency_key": "stable-request-key",
  "evidence_refs": [{
    "source_ref_id": "src_<content-addressed-id>",
    "source_content_hash": "sha256:<source-digest>"
  }]
}
```

`target_object_id`를 쓰는 경우 `target_memory_id`는 생략한다. 둘 중 정확히 하나만 허용한다. `approved_by`, `approved=true`, 임의 `proposed_value`는 입력 계약에 없다.

## 결정과 재조회

새 카드는 PostgreSQL의 non-authoritative proposal로 준비한다. authority 트랜잭션 안에서 target revision/currentness와 source를 다시 확인한 뒤, successor의 `auto_accepted`, 이전 카드의 `superseded`, `correct_fact` 감사, 명시적 `supersedes` edge와 projection outbox를 함께 commit한다. 중간 SQL 실패는 authority 변경 전체를 rollback한다. 실패한 준비 proposal은 review 가능한 비권위 상태로 남을 수 있다.

동일 idempotency key와 동일 요청은 저장된 결정 결과를 재사용한다. key를 재사용하면서 target/evidence/policy를 바꾸면 conflict다. 성공 후 cache를 비우고 `brain.resolve(mode=list)`로 값을 확인한다. commit 이후 재조회 장애는 `committed_readback_gap`으로 반환하므로 write가 없었던 것으로 오인하지 않는다.

거절된 교정은 `authority_write_performed=false`, typed reason, hash만 포함한 `steward_correction_denials` 감사와 review proposal을 반환한다. 활성화된 동일 project/repository 정책의 실패 proposal은 기존 ledger review queue에 남긴다. source body, private path, bearer는 응답·감사에 넣지 않는다. 성공 결정의 immutable snapshot은 `steward_card_decisions`에 보존한다.

## goal 완료 증거

구현과 단위 test만으로 완료하지 않는다. source commit → build content hash → image digest → Ubuntu deployed MCP identity가 연결되어야 한다. 격리 synthetic object 하나에 대해 실제 LLM이 조회 → 독립 source 확인 → MCP 교정 호출 → 동일 route 재조회를 수행해야 한다. stale/unverifiable/conflicting/cross-scope/CAS 경로는 canonical before/after와 denial audit로 무변경을 입증한다.

검증 뒤 synthetic current object를 audit를 보존하는 non-current 상태로 정리하고 다시 조회한다. 운영 활성화 여부와 canary 검증 여부를 명시하며, 사용자 memory를 변경하지 않았다는 postcheck를 남긴다. live receipt와 운영값은 `neurons-ops` 또는 private storage에만 저장한다.

2026-10-25 초기 adapter의 source·설치 artifact·Ubuntu 격리 배포 HTTP/stdio·실제 LLM 검증 결과는 [검증 보고서](validation/2026-10-25-lbrain-evidence-correction.md)에 기록했다. 운영 활성화는 아직 수행하지 않았다.
