# 제출 전 정적 검사

`api` Spring profile의 `POST /v1/ingest/validate`는 기존 `/v1/ingest/enqueue`와 같은 `EnqueueRequest` JSON을 받는다. 이미 redaction된 제출 파일을 기존 보호된 API 주소에 보내는 예:

```sh
curl --fail-with-body -H 'Content-Type: application/json' \
  --data-binary @redacted-enqueue.json \
  "$INGRESS_API_URL/v1/ingest/validate"
```

| HTTP | 응답 |
|---|---|
| 200 | `{"status":"valid","errors":[]}` |
| 400 | `{"status":"rejected","errors":["request rejected"]}` 또는 기존 고정 schemaVersion 오류 메시지 |
| 422 | `{"status":"unsupported_payload","errors":["redacted_document_ref is reserved but disabled"]}` |

JSON 파싱 실패·본문 누락은 기존 Spring MVC 400 처리 계약을 사용한다. 입력 본문이나 원래 필드값을 검사 응답에 포함하지 않는다.

검사는 schemaVersion, 필수값, source, contentHash, 지원 kind·targetProfile, 기존 redaction 규칙에 한정한다. 예약된 `redacted_document_ref`는 enqueue와 같이 422로 거부한다. 정상 결과에도 jobId나 입력값은 반환하지 않는다.

**`valid`는 접수 성공 보증이 아니다.** 검사 경로는 publisher, idempotency store(변경 가능한 `conflicts()` 포함), status service, DB, 외부 backend를 호출하지 않는다. idempotency 충돌, 큐 연결, backend 권한, 최종 인덱싱 성공은 실제 제출 시 별도로 판정된다. 검사를 여러 번 호출해도 key를 예약하지 않는다. 기존 enqueue의 202/400/409/422/503 의미와 동작은 그대로 유지한다.

새 인증·gateway allowlist·공개 노출 설정은 추가하지 않는다. 배포 전 기존 ingress/gateway 보호가 새 경로에도 적용되는지 별도 읽기 전용 증거로 확인해야 하며, 확인 불가 또는 allowlist 확대 필요 시 배포를 중단한다. 로컬 테스트의 같은 `api` profile 확인은 실제 gateway 보호 증명이 아니다.
