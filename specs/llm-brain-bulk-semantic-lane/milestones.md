# Milestones — llm-brain-bulk-semantic-lane

## M1 Hot-path episode-only flip
- status: done
- evidence: tests/test_graph_trigger_cli.py 5 passed; 기본 child argv에 `--extract-entities` 부재, `extract_entities=True` opt-in시 존재

## M2 Bulk trigger wrapper + 등록
- status: done
- evidence: tests/test_bulk_semantic_trigger_cli.py 5 passed; `couchdb-bulk-semantic-trigger` 등록(help 노출); dry-run CLI smoke = status=dry_run, child=couchdb-graph-bulk-semantic, lock=graph-project.lock, mutation/network/raw_paths 모두 False; frozen child 회귀(test_couchdb_graph_bulk_semantic_cli.py) green
- replan: per-call 크기는 child<-env 권위 보존(wrapper sentinel 0=생략). 설계의 "5+1 env 이미 배선" 원칙과 일치

## M3 compose 서비스 + env + runbook
- status: done
- evidence: compose에 off-by-default `llm-brain-bulk-semantic-trigger`(profile llm-brain-bulk-semantic, 공유 runtime-dir/lock) 추가; GRAPH_EXTRACT_ENTITIES 기본 false flip(:247,:301,.env.example); BULK_SEMANTIC_* env 문서화; runbook 섹션; gradle ComposeConfigTest green(서비스/프로필/hot-path flip 텍스트 단언 통과)
- divergence: gradle ComposeConfigTest가 HEAD에서 이미 red였음 — mcp 서비스가 의도적·문서화된 `network_mode: host`를 쓰는데 `doesNotContain("network_mode: host")` 단언이 stale. 기능과 무관한 기존 결함. green 복구 위해 stale 단언 제거(테스트 1줄), 사용자에게 보고

## M4 Verification
- status: done (로컬 + 라이브 smoke)
- evidence(로컬): worker targeted 29 passed; full worker 1143 passed/9 skipped; gradle 139 passed; `--show-boundary` 정상
- evidence(라이브 ops-host, 브랜치 worker 이미지 일회성 컨테이너):
  - dry-run: `--network none`에서 status=dry_run, mutation/network/raw_paths 모두 False, schema=llm_brain_bulk_semantic_trigger.v1
  - graph status: 라이브 PG/CouchDB/projection-state 상대 read-only status=ok
  - 모델 정책: Gemma-4(gemma-4-26b-a4b-it-maas) chat 200·응답모델 일치, embedding gemini-embedding-2 200·dim 3072 라우팅 확인(Gemini/Flash 미사용)
  - bounded execute(`--max-projects 1`, 5건·500건 스캔): trigger/child status=ok, 전부 skipped_resumed(이미 entity-projected), failed=0, public-safe. 코퍼스 M7 drain 소진으로 신규 write 0 → write 로직은 worker 유닛테스트가 커버
- rollback: 라이브 그래프/원장에 쓴 것 0(materialized=0). staging 이미지/디렉터리 제거, data 서비스(neo4j/couchdb/pg) uptime 불변=무재시작. 라이브 brain 시작 상태 그대로
- 라이브 lane 상시 기동(off-by-default profile)은 미실행 — 정상 merge→deploy 경로의 운영 결정
