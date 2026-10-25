"""격리 runtime 전용 재검토 실증 연결기. 운영 CLI로 등록하지 않는다.

synthetic Codex 형식 자료를 기존 parser/ingress에 넣고, 기존 JSON 정책
adapter와 재검토 코어를 연결한다. 일반 자유문장 교정의 완료 증거가 아니다.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from agent_knowledge.knowledge_search_service import DisabledRetiredIndexBridgeClient, KnowledgeSearchService
from agent_knowledge.evidence_correction import CorrectionError, load_policies, resolve_value
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.models import SourceRefRecord
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.public_safe_util import hash_payload, sha256_text, short_hash, stable_json
from agent_knowledge.rag_ingress.state_db import RAGIngressStateDB
from agent_knowledge.rag_ingress.state_sink import StateDBIngressSink
from agent_knowledge.recheck_scheduler import (
    PgRecheckQueue, RecheckBlocked, RecheckOutcome, RecheckSnapshot, RecheckWorker, inspect_recheck_health,
)
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.tool_evidence_sync import ToolEvidenceSyncRunner
from agent_knowledge.session_memory.transcript_ingest import TranscriptIngestWorker
from agent_knowledge.session_memory.transcript_model import ToolEvidenceSummaryRecord
from agent_knowledge.session_memory.transcript_parsers import extract_tool_evidence, parse_transcript_source


def now():
    return datetime.now(timezone.utc).isoformat()


class Probe:
    def __init__(self, state: Path, project: str):
        if os.environ.get("LBRAIN_RECHECK_PROBE_ACK") != "synthetic_only" or not project.startswith("synthetic-recheck-"):
            raise ValueError("synthetic probe acknowledgement and project required")
        self.state, self.project = state, project
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state.stat().st_mode & 0o077:
            raise ValueError("private probe state directory required")
        self.store = PgVectorStore(dsn=os.environ["NEURON_LBRAIN_PGVECTOR_DSN"])
        with self.store._scope() as db:
            row = db.execute("SELECT current_database() AS name").fetchone()
            if not row["name"].startswith("neurons_recheck_proof"):
                raise ValueError("dedicated synthetic database required")
            row = db.execute("SELECT to_regclass('memory_cards') AS exists").fetchone()
            if row["exists"]:
                count = db.execute("SELECT count(*) AS n FROM memory_cards WHERE project NOT LIKE 'synthetic-recheck-%%'").fetchone()["n"]
                if count:
                    raise ValueError("non-synthetic data present")
        self.ledger = Ledger(state / "ledger.sqlite")
        self.queue = PgRecheckQueue(self.store)

    def write(self, name, value):
        path = self.state / name
        temporary = path.with_suffix(path.suffix + ".new")
        temporary.write_text(json.dumps(value, ensure_ascii=False, default=str))
        temporary.replace(path)

    def metadata(self):
        return json.loads((self.state / "metadata.json").read_text())

    def source(self, name, body, *, observed_at=None):
        encoded = stable_json(body)
        data = dict(root_id="synthetic-recheck-sources", device_id_hash=sha256_text("synthetic-recheck-server"),
            relative_path_hash=sha256_text(name), content_hash=sha256_text(encoded),
            mtime=observed_at or now(), size=len(encoded.encode()), sync_policy="full_sync",
            permission_scope="project", last_seen_at=observed_at or now(), redacted_content=encoded)
        data["source_ref_id"] = "src_" + short_hash([
            data["device_id_hash"], data["root_id"], data["relative_path_hash"], data["content_hash"]])
        return SourceRefRecord(**data)

    def seed(self):
        self.store.execute_ddl()
        if (self.state / "metadata.json").exists():
            raise ValueError("probe already seeded")
        steward = BrainStewardService(self.ledger, pgvector_store=self.store, allow_restricted=True)
        span = dict(card_type="status", project=self.project, provider="codex", scope="project",
            title="Synthetic recheck test result", redacted_summary="테스트 상태: missing",
            typed_payload={"status_value": "missing", "observed_at": now(), "expires_at": "", "current_authority": "synthetic-probe-tests"},
            source_ref={"source_id": "synthetic-seed"}, span_ref={"span_id": "synthetic-seed-span"},
            content_hash=sha256_text("synthetic-recheck-seed"), confidence=0.95, confidence_basis="격리 실증 준비")
        memory_id = steward.candidate_create(source_span=span)["memory_id"]
        steward.candidate_approve(candidate_memory_id=memory_id, approved_by="synthetic-seed", decision_id="seed:" + self.project)
        old = self.store.get_steward_card(memory_id)
        catalog = LedgerSourceRefCatalog(self.ledger)
        records = [self.source("source-a", {"tool_evidence": {"outcome": "pass"}}),
                   self.source("source-b", {"tool_evidence": {"outcome": "fail"}})]
        catalog.register_all(records)
        sources = [{"adapter": "registered_json", "root_id": r.root_id, "device_id_hash": r.device_id_hash,
            "relative_path_hash": r.relative_path_hash, "json_pointer": "/tool_evidence/outcome"} for r in records]
        rule = dict(policy_id="synthetic-test-result", project=self.project, repository="neurons", memory_id=memory_id,
            field_path="typed_payload.status_value", summary_template="테스트 상태: {value}",
            sources=sources, max_age_seconds=3600, value_map={})
        self.write("policy.json", {"schema_version": "evidence_correction_policy.v1", "rules": [rule]})
        correction = dict(project=self.project, repository="neurons", target_memory_id=memory_id,
            expected_content_hash=old["content_hash"], policy_id=rule["policy_id"], idempotency_key="probe")
        task = self.queue.register(project=self.project, memory_id=memory_id, request={
            "correction": correction, "judge_profile": "structured-policy-probe.v1"},
            request_key="synthetic-source-conflict", delay_seconds=3)
        self.write("metadata.json", {"task_id": task["task_id"], "project": self.project, "memory_id": memory_id})
        return {"seeded": True, "task_id": task["task_id"], "next_check_at": task["next_check_at"]}

    def service(self, store=None):
        return KnowledgeSearchService(ledger=self.ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(),
            dataset_ids=[], pgvector_store=store or self.store, allow_evidence_backed_object_corrections=True,
            evidence_correction_policies=load_policies(str(self.state / "policy.json")))

    def stage(self):
        # 실행 결과를 먼저 만들되, ingest 전에는 재검토 source로 등록하지 않는다.
        test = self.state / "test_probe.py"
        test.write_text("def test_probe():\n    assert sum([1, 2, 3]) == 6\n")
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)]
        execution = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if execution.returncode != 0:
            raise RuntimeError("synthetic probe test failed")
        stamp = now()
        output = execution.stdout + execution.stderr
        records = [
            {"timestamp": stamp, "type": "session_meta", "payload": {"id": "synthetic-recheck-native-session"}},
            {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run the synthetic probe test."}]}},
            {"timestamp": stamp, "type": "response_item", "payload": {"type": "function_call", "call_id": "synthetic-probe-call", "name": "exec_command", "arguments": json.dumps({"cmd": " ".join(command)})}},
            {"timestamp": stamp, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "synthetic-probe-call", "output": output}},
            {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The synthetic probe test completed successfully."}]}},
        ]
        source = self.state / "synthetic-session.jsonl"
        source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return {"staged": True, "actual_test_exit_code": execution.returncode,
                "source_hash": sha256_text(source.read_text()), "ingested": False}

    def ingest(self):
        source = self.state / "synthetic-session.jsonl"
        locator = sha256_text("synthetic-probe-locator")
        sink = StateDBIngressSink(state_db=RAGIngressStateDB(self.state / "ingress.sqlite"))
        parsed = parse_transcript_source("codex", source, project=self.project, source_locator_hash=locator)
        result = TranscriptIngestWorker(capture_spool=None, ledger=self.ledger, enqueue_sink=sink).index_parsed_transcript(
            request={"request_id": "synthetic-probe-ingest"}, parsed=parsed)
        tool_result = ToolEvidenceSyncRunner(ledger=self.ledger, enqueue_sink=sink).run(
            provider="codex", source_path=source, project=self.project, source_locator_hash=locator)
        records = extract_tool_evidence("codex", source, project=self.project, source_locator_hash=locator)
        if len(records) != 1 or records[0].category != "test_result" or records[0].outcome != "pass":
            raise ValueError("expected one admitted passing synthetic test evidence")
        # 실험용 연결기: 기존 parser 출력과 기존 ledger 저장 API를 연결한다.
        # 운영 수집기에 자동으로 설치되는 기능으로 주장하지 않는다.
        for record in records:
            self.ledger.upsert_tool_evidence_summary(record=record)
        return {"ingested": True, "conversation_status": result.status, "tool_evidence_count": len(records),
                "tool_documents_enqueued": tool_result["enqueued"], "source_hash": sha256_text(source.read_text())}

    def load(self, task):
        if (self.state / "source-unavailable").exists():
            raise ConnectionError("synthetic source outage")
        if (self.state / "configuration-missing").exists():
            raise RecheckBlocked("configuration_missing")
        rows = self.ledger.list_tool_evidence_summaries(project=self.project, provider="codex")
        if rows:
            if len(rows) != 1:
                raise ValueError("ambiguous synthetic evidence")
            row = rows[0]
            evidence = ToolEvidenceSummaryRecord(**{key: row[key] for key in (
                "session_id_hash", "provider", "project", "category", "outcome", "tool_name",
                "command_summary", "redacted_summary", "observed_at", "evidence_index", "redaction_version", "source_status")})
            if evidence.evidence_id_hash != row["evidence_id_hash"] or evidence.content_hash != row["content_hash"]:
                raise ValueError("synthetic tool evidence hash mismatch")
            cursor_path = self.state / "source-cursor.json"
            previous = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
            if previous.get("evidence_id_hash") != evidence.evidence_id_hash:
                record = self.source("source-b", {"tool_evidence": asdict(evidence)}, observed_at=evidence.observed_at)
                LedgerSourceRefCatalog(self.ledger, initialize_schema=False).register(record)
                self.write("source-cursor.json", {"evidence_id_hash": evidence.evidence_id_hash,
                    "source_ref_id": record.source_ref_id, "source_content_hash": record.content_hash})
        rule = load_policies(str(self.state / "policy.json"))[0]
        catalog = LedgerSourceRefCatalog(self.ledger, initialize_schema=False)
        refs = []
        for binding in rule["sources"]:
            records = catalog.list_for_path(**{key: binding[key] for key in ("root_id", "device_id_hash", "relative_path_hash")})
            latest = max(records, key=lambda record: datetime.fromisoformat(record.last_seen_at))
            refs.append({"source_ref_id": latest.source_ref_id, "source_content_hash": latest.content_hash})
        return RecheckSnapshot.from_data(task, {"policy_hash": hash_payload(rule), "evidence_refs": refs,
            "judge_profile": task["request"]["judge_profile"]})

    def judge(self, task, snapshot):
        with (self.state / "judgment-calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": now(), "input_hash": snapshot.fingerprint, "profile": "structured-policy-probe.v1"}) + "\n")
        rule = load_policies(str(self.state / "policy.json"))[0]
        try:
            value, _ = resolve_value(rule=rule, catalog=LedgerSourceRefCatalog(self.ledger, initialize_schema=False),
                                     evidence_refs=snapshot.data["evidence_refs"])
        except CorrectionError as exc:
            return {"action": "wait", "reason_code": exc.reason, "profile": "structured-policy-probe.v1"}
        return {"action": "apply", "reason_code": "source_verified", "verified_value": value,
                "profile": "structured-policy-probe.v1"}

    def crash_once(self, marker):
        path = self.state / marker
        if path.exists():
            path.replace(self.state / (marker + ".consumed"))
            os._exit(91)

    def apply(self, task, snapshot, judgment):
        self.crash_once("crash-before-apply")
        if judgment["action"] != "apply":
            return RecheckOutcome("waiting", judgment["reason_code"])
        request = dict(task["request"]["correction"], evidence_refs=snapshot.data["evidence_refs"],
            idempotency_key="probe-" + task["task_id"] + "-" + snapshot.fingerprint[7:])
        with self.queue.owned_transaction(task) as db:
            result = self.service(PgVectorStore(connection=db)).brain_object_correct(**request)
            if result["correction_status"] not in {"committed", "committed_readback_gap"}:
                return RecheckOutcome("waiting", result["reason_code"])
        self.write("last-correction.json", result)
        self.crash_once("crash-after-commit")
        if not result["read_after_write_verified"]:
            return RecheckOutcome("waiting", "readback_gap")
        return RecheckOutcome("resolved", "authority_verified", result["decision_id"])

    def worker(self):
        worker_id = "probe-" + str(os.getpid()) + "-" + short_hash([now()])
        worker = RecheckWorker(queue=self.queue, project=self.project, worker_id=worker_id,
            load=self.load, judge=self.judge, apply=self.apply, lease_seconds=6, retry_seconds=1, max_retry_seconds=2)
        stopped = False

        def stop(*args):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        while not stopped:
            event = worker.run_once()
            if event["claimed"]:
                with (self.state / "worker-events.jsonl").open("a") as stream:
                    stream.write(json.dumps({"at": now(), **event}) + "\n")
            time.sleep(0.2)

    def monitor(self):
        stopped = False

        def stop(*args):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        while not stopped:
            health = inspect_recheck_health(self.queue, project=self.project, heartbeat_grace=2, overdue_grace=2)
            with (self.state / "monitor-events.jsonl").open("a") as stream:
                stream.write(json.dumps({"at": now(), **health}) + "\n")
            self.write("monitor-status.json", {"at": now(), **health})
            time.sleep(0.3)

    def snapshot(self):
        task = self.queue.get(self.metadata()["task_id"])
        with self.store._scope() as db:
            decisions = db.execute("SELECT count(*) AS n FROM steward_card_decisions WHERE action='correct_fact'").fetchone()["n"]
            count = db.execute("SELECT count(*) AS n FROM memory_cards WHERE project NOT LIKE 'synthetic-recheck-%%'").fetchone()["n"]
            attempts = db.execute("SELECT * FROM lbrain_recheck_attempts WHERE task_id=%s ORDER BY generation", (task["task_id"],)).fetchall()
        cards = self.store.list_steward_cards(project=self.project, accepted_only=True, current_only=True, limit=100)
        return {"at": now(), "task": task, "attempts": attempts, "correct_fact_decisions": decisions,
                "non_synthetic_card_count": count, "current": [{"id": card["memory_id"],
                    "status": card["typed_payload"]["status_value"]} for card in cards],
                "judgment_calls": len((self.state / "judgment-calls.jsonl").read_text().splitlines())
                    if (self.state / "judgment-calls.jsonl").exists() else 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["seed", "stage", "ingest", "worker", "monitor", "snapshot", "read"])
    parser.add_argument("--state", type=Path, default=Path("/canary"))
    parser.add_argument("--project", default="synthetic-recheck-01a16e76")
    args = parser.parse_args()
    probe = Probe(args.state, args.project)
    if args.mode == "read":
        result = probe.service().brain_memory_resolve(project=args.project, mode="context", query="", response_mode="slim", limit=20)
    else:
        result = getattr(probe, args.mode)()
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error_code": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
