# Native 관측 출처 보존과 실제 Couch/PG intake 부분 검증

전체 교정 goal은 진행 중이다. 이 기록은 기존 세션 source에서 miner 후보까지 관측 참조가 보존되는 범위만 입증한다. native 관측으로 기존 기억을 자동 교정하거나 모든 writer가 합류했다는 의미가 아니다.

## 구현 범위

기존 Codex extractor의 native event hash, source content hash, observation ID, evidence ID, extractor/redaction version을 tool evidence bundle에 전달한다. 같은 native 호출의 관측 ID는 rechunk와 extractor 변경에도 유지한다. 다른 호출은 같은 출력이어도 합치지 않는다. 해석 version은 별도 evidence ID와 source revision으로 보존한다. native anchor가 없는 옛 기록에 ID를 추정 부여하지 않는다.

관측 참조는 redacted summary의 정확한 span/hash에 연결한다. 기존 Couch immutable source와 active revision manifest를 재사용하며 새로운 세션 본문 저장소는 만들지 않는다. materialization은 참조의 좌표를 canonical session body로 변환한다. hash와 active source는 무결성과 출처 admission이며, 해당 사실이 현재 참임을 인증하지 않는다.

PG의 `session_memory_chunk_sources`는 기존 chunk와 여러 source revision을 연결하는 불변 metadata다. 같은 본문/벡터를 재사용해도 새 source revision과 관측 참조를 같은 transaction에 저장한다. canonical body가 별도 마스킹 representation과 달라도 native span 좌표는 canonical source 기준임을 명시한다. FK에 cascade 삭제를 추가하지 않았다. 교정 이력 전체의 retention/GC 정책은 아직 남아 있다.

`CouchObservationCatalog`는 active pinned source, project/provider/session 범위, span/hash, 서버가 지정한 extractor/redaction version을 확인한다. source pointer는 admission 후 다시 확인한다. PG 행의 도착 순서로 source를 선택하지 않는다. PG에 active revision의 정확한 연결이 없으면 처리를 거부한다. miner 입력은 기존 canonical materialization에서 읽으며 PG representation의 scope/hash도 대조한다.

실제 `pg-memory-propose` 진입점은 `--native-source-policy-file`을 지정한 PG 모드에서 이 경로를 사용한다. Couch 설정은 기존 `COUCHDB_URL`, `COUCHDB_DB`, `COUCHDB_USER`, `COUCHDB_PASSWORD` 계약을 따른다. 기존 JSON/PG 모드의 호환성은 유지한다. 정책 파일 예시는 다음과 같다.

```json
{
  "schema_version": "native_source_policy.v1",
  "extractor_versions": {"codex": "codex-tool-evidence-extractor.v2"},
  "redaction_version": "redaction.v2"
}
```

single/batch miner는 실제로 보여준 source span의 `evidence_id_hashes`만 후보에 연결할 수 있다. 다른 source나 잘린 구간의 ID를 지정하면 거부한다. 선택하지 않은 관측을 후보의 근거로 자동 첨부하지 않는다. 후보의 source reference와 checkpoint 처리 키에는 source revision이 포함되므로 같은 본문을 새 extractor로 해석한 입력을 누락하지 않는다. 이 선택 자체가 의미상 지지를 검증한 것은 아니다.

## 실제 검증

Ubuntu의 작업 전용 CouchDB 3.4와 PostgreSQL, 작업 전용 검증 컨테이너에서 합성 native trace/source만 사용했다. 운영 서비스나 private 세션 본문은 사용하지 않았다. 초기 Couch 설정 mount 기동 오류를 수정한 뒤 전체 해당 검사를 재실행했다. 테스트 인증값은 실패 출력 이후 교체했고 로그를 가렸으며 이후 실행은 짧은 traceback을 사용했다.

- 실제 Couch HTTP + PG + projection/representation: `uv run --no-project pytest -q --tb=short -c /proof/pyproject.toml /proof/tests/test_observation_provenance.py /proof/tests/test_pg_backfill.py /proof/tests/test_pg_representation.py` → **92 passed, 1 warning in 20.29s**.
- 실제 HTTP 검사에서 extractor v2→v3의 본문 불변/새 source revision, 누락된 PG link 거부, 옛 extractor 정책 거부, 새 revision별 checkpoint key, 벡터 1회 생성, 이전 immutable source 보존을 확인했다.
- projection link 저장 뒤 실패를 주입해 chunk와 link가 함께 rollback되는 것을 실제 PG에서 확인했다.
- 해당 source bundle SHA-256: `3fc44a2b4e46eb2a4bfd45b77645d797d7c41601f363f375498792bb8608b08d`.
- Couch image digest: `sha256:d9b6609989f1d47822e84005946cecc2fd49e4be5e4f35da960d4379f6c60de0`.
- 검증용 의존성 image digest: `sha256:ee9b21c07899568b49342da9b884adb84847fd02c0f7583f0464d000fa155f6a`. source는 읽기 전용 주입했으므로 최종 제품 image identity 증거가 아니다.
- 로컬 miner/intake 관련 검사: **97 passed, 3 skipped in 0.22s**. 이때 skip된 실제 Couch/PG 검사는 위 Ubuntu 실행에서 별도로 실행했다.
- worker 전체 `uv run pytest -q`: **5258 passed, 653 skipped, 4 warnings in 55.08s**. 환경 선택형 skip은 실증으로 계산하지 않는다.
- Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**. `git diff --check`: 통과.

실제 embedding provider 호출 대신 명시적 synthetic embedder를 사용했다. 이는 source identity와 벡터 재사용 증거이며 embedding provider 자체의 동작 증거가 아니다. 이 첫 검사만으로 운영 스케줄러, 기존 fact의 영향 재검토나 전체 goal의 완료를 입증하지 않는다. 후속 연결 범위는 아래에 구분한다.

## 정규 intake의 공통 fact queue 연결

후속 변경에서 `BrainStewardService.candidate_autopilot_accept`와 실제 `pg-memory-propose --fact-correction-policy-file`에 공통 coordinator를 연결했다. MCP service도 구성된 동일 coordinator를 steward에 전달한다. 서버 정책의 정확한 project/title/field에 대응하는 기존 사실만 대상으로 한다. 관리 대상 후보는 disabled proposal로 보존하고, 별도 accepted/current 기억으로 승격하지 않는다.

native 관측 참조는 queue의 `trigger`에 남긴다. 이 참조는 검토를 촉발한 입력이며, 독립적인 현재 사실의 증거나 Git 관측과 같은 관측이라는 뜻이 아니다. 실제 적용 값은 서버가 현재 Git JSON source에서 별도로 확인한다. 제안 값이 오래됐거나 틀렸어도 그 값으로 현재 기억을 역전시키지 않는다. 이 경로는 일반 자연어 근거의 의미상 지지를 검증하는 경로가 아니다.

동일 후보·동일 정책 version은 target의 successor가 바뀌어도 기존 queue 작업을 반환한다. JSON route key의 unique index와 authority lock으로 보호한다. 자동 승인 또는 queue 등록이 실패하면 intake가 source 완료 checkpoint를 기록하지 않는다. 정상적으로 queue에 들어간 경우에만 checkpoint를 전진시키고 이후 처리는 내구성 worker가 맡는다. 보류 후보 전체를 자동으로 재평가하는 운영 정책이 이 연결만으로 완성되는 것은 아니다.

- 실제 Couch HTTP/PG/Git + 정규 intake + steward + 공통 queue/authority 검사: **115 passed, 1 warning in 47.40s**. `test_native_fact_intake.py`, `test_fact_resolution.py`, `test_observation_provenance.py`, `test_brain_steward.py`, `test_candidate_intake_cli.py`를 Ubuntu에서 실행했다.
- 두 순서인 교정→지연 native 수집과 native 수집→교정을 확인했다. 최초 승인 포함 canonical 결정은 두 건이며 현재 값은 `ready` 하나다. 지연 후보의 제안 `missing`은 별도로 보존한다. 같은 Git 관측 assertion은 한 건이며 native 후보는 disabled 상태다.
- 완료 뒤 같은 후보를 다시 자동 승인 요청해도 기존 resolved task를 반환하고 새 queue 작업이나 accepted 중복을 만들지 않는다. source checkpoint 재실행은 추가 miner 호출도 만들지 않는다.
- 이 검사에서 miner는 명시적 응답 대역이며 실제 OpenRouter 모델의 의미 판단 증거가 아니다. native source와 Git 사실을 동일 관측으로 병합한 것도 아니다.
- source bundle SHA-256: `0b414933de0e016a9005370b7596a68e76f579335e480caf5533bf3033de7bff`. 의존성 image와 source 주입 방식의 한계는 위와 같다.
- Ubuntu에서 추가로 드러난 기존 steward 검사 기대값을 실제 계약과 맞췄다. `brain_id`와 이미 제공하던 review 분류 필드를 정확히 검사하며, project 일치와 current 충돌 상대도 확인한다. 제품 필드를 삭제하거나 검사를 skip하지 않았다.
- 후속 변경의 worker 전체 `uv run pytest -q`: **5259 passed, 655 skipped, 4 warnings in 57.74s**. `git diff --check` 통과. Java source는 변경하지 않았다.

정확한 서버 정책에 아직 연결되지 않은 신규 중복, 다른 writer의 승인, 일반 의미 target 검색, native 근거 자체의 현재 사실 검증, 철회/파생물/retention/실제 모델과 최종 운영 identity는 남아 있다.
