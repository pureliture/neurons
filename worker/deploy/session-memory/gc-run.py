"""session-memory GC 1회(컨테이너). 호스트 gc-run.py의 컨테이너-네이티브 버전.

3개 GC(session_memory/transcript_volume/transcript_session)를 backup→delete→audit로
실행(--execute). RetiredIndexBridge base는 env. ledger는 NEURON_LEDGER_PG_DSN으로 PG. 패키지 설치됨.
backup-before-delete + abort-on-failure는 각 GC가 보장.
"""
import os, io, json, contextlib, pathlib
from datetime import datetime, timezone

from agent_knowledge.index_client import RetiredIndexBridgeHttpClient
from agent_knowledge.session_memory.dirty_session_memory_sync import resolve_dataset_id as _resolve_dataset_id
from agent_knowledge.session_memory.session_memory_gc import main as sgc, SESSION_MEMORY_GC_OPERATION
from agent_knowledge.session_memory.transcript_volume_gc import main as vgc, TRANSCRIPT_VOLUME_GC_OPERATION
from agent_knowledge.session_memory.transcript_session_gc import main as ssgc, TRANSCRIPT_SESSION_GC_OPERATION

base = os.environ.get("RETIRED_INDEX_BRIDGE_BASE_URL", "http://127.0.0.1:9380")
token = os.environ["RETIRED_INDEX_BRIDGE_API_KEY"]
r = RetiredIndexBridgeHttpClient(base_url=base, bearer_token=token, request_timeout_seconds=45)


def approve(path, op, ds, argv):
    pathlib.Path(path).write_text(json.dumps({
        "schema_version": "agent_knowledge_live_approval.v1", "operation": op,
        "operator_approval": {"approved": True, "by": "operator-standing-autopilot-container"},
        "redaction_required": True,
        "rollback_or_abort_criteria": "bounded auto GC; backup-before-delete(복구가능); abort on failed_count>0; restore via gc_backup",
        "timeout_seconds": 1200, "target": {"dataset_id": ds, "index_base_url": base},
        "command": {"argv": argv},
    }))


def run(fn, argv):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        fn(argv)
    return json.loads(b.getvalue().strip().splitlines()[-1])


out = {"ts": datetime.now(timezone.utc).isoformat(), "mode": "auto_execute_container"}
sds = _resolve_dataset_id(retired_index_bridge=r, dataset_name="session-memory")
tds = _resolve_dataset_id(retired_index_bridge=r, dataset_name="transcript-memory")
sa = ["--ledger", "state/neuron-ledger.sqlite", "--dataset-id", sds, "--retired-index-bridge-url", base,
      "--max-items", "25", "--min-disabled-age-seconds", "86400", "--backup-dir", "state/gc-backup",
      "--execute", "--approval", "state/gc-approval-session.json"]
approve("state/gc-approval-session.json", SESSION_MEMORY_GC_OPERATION, sds, ["session-memory-gc"] + sa)
s = run(sgc, sa); out["session"] = {k: s.get(k) for k in ("eligible_count", "deleted_count", "backed_up_count", "status")}
va = ["--ledger", "state/neuron-ledger.sqlite", "--transcript-dataset-id", tds, "--retired-index-bridge-url", base,
      "--max-items", "25", "--min-active-age-seconds", "86400", "--backup-dir", "state/gc-backup",
      "--execute", "--approval", "state/gc-approval-volume.json"]
approve("state/gc-approval-volume.json", TRANSCRIPT_VOLUME_GC_OPERATION, tds, ["transcript-volume-gc"] + va)
v = run(vgc, va); out["transcript_volume_edge"] = {k: v.get(k) for k in ("eligible_count", "deleted_count", "status")}
ta = ["--transcript-dataset-id", tds, "--session-memory-dataset-id", sds, "--retired-index-bridge-url", base,
      "--max-items", "100", "--min-transcript-age-seconds", "86400", "--backup-dir", "state/gc-backup",
      "--execute", "--approval", "state/gc-approval-session-level.json"]
approve("state/gc-approval-session-level.json", TRANSCRIPT_SESSION_GC_OPERATION, tds, ["transcript-session-gc"] + ta)
t = run(ssgc, ta); out["transcript_session_level"] = {k: t.get(k) for k in ("summarized_session_count", "eligible_count", "deleted_count", "backed_up_count", "status")}
failed = any(x.get("status") not in ("ok",) for x in (s, v, t))
out["needs_attention"] = failed
alert = pathlib.Path("state/gc-eligible-alert.json")
(alert.write_text(json.dumps(out)) if failed else (alert.unlink(missing_ok=True)))
print(("GC_RUN_ALERT " if failed else "GC_RUN_OK ") + json.dumps(out))
