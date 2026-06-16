"""transcript backfill 1회(컨테이너). 호스트 backfill.sh의 컨테이너-네이티브 버전.

resolve_dataset_id는 dirty_session_memory_sync에서(신 lib API). RAGFlow base는 env.
"""
import os, io, json, contextlib

from agent_knowledge.ragflow_client import RagflowHttpClient
from agent_knowledge.session_memory.dirty_session_memory_sync import resolve_dataset_id as _resolve_dataset_id
from agent_knowledge.session_memory.transcript_backfill import main as bf

base = os.environ.get("RAGFLOW_BASE_URL", "http://127.0.0.1:9380")
r = RagflowHttpClient(base_url=base, bearer_token=os.environ["RAGFLOW_API_KEY"], request_timeout_seconds=45)
TX = _resolve_dataset_id(ragflow=r, dataset_name="transcript-memory")
SM = _resolve_dataset_id(ragflow=r, dataset_name="session-memory")
b = io.StringIO()
with contextlib.redirect_stdout(b):
    bf(["--ledger", "state/neuron-ledger.sqlite", "--transcript-dataset-id", TX,
        "--session-memory-dataset-id", SM, "--ragflow-url", base, "--max-sessions", "500"])
print(b.getvalue().strip().splitlines()[-1])
