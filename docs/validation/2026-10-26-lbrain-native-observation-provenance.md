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

실제 embedding provider 호출 대신 명시적 synthetic embedder를 사용했다. 이는 source identity와 벡터 재사용 증거이며 embedding provider 자체의 동작 증거가 아니다. 운영 스케줄러 설치, source 철회/재해석의 기존 fact 영향 재검토, native-to-fact 공통 authority 연결과 전체 goal 완료는 남아 있다.
