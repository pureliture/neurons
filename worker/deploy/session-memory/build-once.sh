#!/bin/bash
# session-memory builder 1사이클(컨테이너). 호스트 run.sh의 컨테이너-네이티브 버전.
# 차이: docker cp 대신 마운트된 shadow 볼륨에서 복사, RAGFlow는 host.docker.internal,
# 패키지는 pip install 됨(PYTHONPATH 불필요), ledger는 NEURON_LEDGER_PG_DSN으로 PG.
set -e
cd /app
mkdir -p state/runtime
chmod 700 state state/runtime 2>/dev/null || true
# shadow ingest-state(dirty signal source): rag-ingress 볼륨(ro)에서 스냅샷.
if [ -f /shadow/ingest-state.sqlite ]; then
  cp /shadow/ingest-state.sqlite state/shadow-snap.sqlite
fi
python -m agent_knowledge.session_memory.neuron_session_memory \
  --ledger state/neuron-ledger.sqlite \
  --dataset-name session-memory \
  --ragflow-url "${RAGFLOW_BASE_URL:-http://127.0.0.1:9380}" \
  --token-env RAGFLOW_API_KEY \
  --runtime-dir state/runtime \
  --shadow-db state/shadow-snap.sqlite \
  --watermark-file state/wm.txt \
  --batch-size 10 \
  --max-processed-per-run 10 \
  --limit 50 \
  --approval state/neuron-build-approval.json
