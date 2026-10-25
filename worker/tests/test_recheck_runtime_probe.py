"""근거 bytes가 같아도 실제 유효성 변화가 cache에 반영되는지 검사한다."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.public_safe_util import sha256_text


def test_probe_expiry_renewal_and_revocation_invalidate_judgment(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("recheck_probe", Path(__file__).parents[1] / "eval/recheck_runtime_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probe = module.Probe.__new__(module.Probe)
    probe.state, probe.project, probe.ledger = tmp_path, "synthetic-recheck-validity", Ledger(tmp_path / "ledger.sqlite")
    catalog = LedgerSourceRefCatalog(probe.ledger)
    start = datetime.now(timezone.utc)
    record = probe.source("source-a", {"tool_evidence": {"outcome": "pass"}}, observed_at=start.isoformat())
    catalog.register(record)
    rule = dict(policy_id="validity", project=probe.project, repository="neurons", memory_id="test-memory",
        field_path="typed_payload.status_value", summary_template="상태: {value}", max_age_seconds=60, value_map={},
        sources=[dict(adapter="registered_json", root_id=record.root_id, device_id_hash=record.device_id_hash,
            relative_path_hash=record.relative_path_hash, json_pointer="/tool_evidence/outcome")])
    probe.write("policy.json", {"schema_version": "evidence_correction_policy.v1", "rules": [rule]})
    task = {"request_hash": sha256_text("validity-test"), "request": {"judge_profile": "structured-policy-probe.v1"}}
    resolve = module.resolve_value
    current = [start]
    monkeypatch.setattr(module, "resolve_value", lambda **kwargs: resolve(**kwargs, now=current[0]))
    fresh = probe.load(task)
    assert probe.judge(task, fresh)["action"] == "apply"
    current[0] += timedelta(seconds=20)
    assert probe.load(task).fingerprint == fresh.fingerprint
    current[0] += timedelta(seconds=50)
    expired = probe.load(task)
    assert expired.fingerprint != fresh.fingerprint
    assert probe.judge(task, expired)["reason_code"] == "source_stale"
    renewed = probe.source("source-a", {"tool_evidence": {"outcome": "pass"}}, observed_at=current[0].isoformat())
    assert renewed.source_ref_id == record.source_ref_id and renewed.content_hash == record.content_hash
    catalog.register(renewed)
    after_renewal = probe.load(task)
    assert after_renewal.data["evidence_refs"] == expired.data["evidence_refs"]
    assert after_renewal.fingerprint != expired.fingerprint
    assert probe.judge(task, after_renewal)["action"] == "apply"
    from dataclasses import replace
    catalog.register(replace(renewed, revoked_at=current[0].isoformat()))
    revoked = probe.load(task)
    assert revoked.fingerprint != after_renewal.fingerprint
    assert probe.judge(task, revoked)["reason_code"] == "source_deleted_or_revoked"
