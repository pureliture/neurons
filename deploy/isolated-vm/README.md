# Isolated single-VM test stack

합성 데이터 전용 격리 스택이다. CouchDB, PostgreSQL/pgvector, Neo4j, Ollama를
loopback 전용 port로 띄운다. 운영 cluster, `neurons-ops`, 실제 데이터와는 연결하지 않는다.

```bash
export ISO_COUCHDB_PASSWORD=... ISO_COUCHDB_SECRET=... ISO_PG_PASSWORD=... ISO_NEO4J_PASSWORD=...
docker compose -f deploy/isolated-vm/compose.yaml up -d --wait
# worker/tests/test_pg_neo4j_card_e2e.py 용 no-auth Neo4j (tmpfs, 일회용)
docker compose -f deploy/isolated-vm/compose.yaml --profile e2e up -d --wait neo4j-e2e
```

- 기본 port: CouchDB 25984, PostgreSQL 25432, Neo4j 27474/27687, Neo4j(e2e) 27688, Ollama 21434
- 모든 port는 `127.0.0.1`에만 bind된다. `ISO_*_PORT`로 바꿀 수 있다.
- live-gated worker 테스트: `LBRAIN_TEST_PG_DSN`, `LBRAIN_TEST_NEO4J_URI=bolt://127.0.0.1:27688`
- 로컬 embedding은 `worker/README.md`의 Model2Vec 절차를 따른다. 외부 LLM API는 사용하지 않는다.
