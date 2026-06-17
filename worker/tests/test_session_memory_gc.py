import hashlib
import json
from datetime import datetime, timedelta, timezone

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory import session_memory_gc as gc_module
from agent_knowledge.session_memory.session_memory_gc import (
    MIN_DISABLED_AGE_FLOOR_SECONDS,
    SessionMemoryGcConfig,
    SessionMemoryGcRunner,
)


PROJECT = "workspace-ragflow-advisor"
SESSION_ID_HASH = "sha256:session-memory-gc-target"


def _backdate_disabled_at(
    ledger: Ledger,
    knowledge_id: str,
    *,
    seconds_ago: int,
    now: datetime | None = None,
) -> None:
    # ``now``를 명시하면 그 기준 시각에서 backdate한다. 기본값(None)은 실 wall clock을
    # 쓴다. runner에 frozen ``now_fn``을 주입하는 테스트는 반드시 같은 frozen 기준을
    # 넘겨야 한다 — 그렇지 않으면 disabled_at(실시각)과 cutoff(frozen-FLOOR)의 비교가
    # 실행 날짜에 따라 흔들려 candidate SQL의 age gate에 걸리거나 걸리지 않는다.
    reference = now if now is not None else datetime.now(timezone.utc)
    stamp = (reference - timedelta(seconds=seconds_ago)).isoformat()
    with ledger._connect() as connection:
        connection.execute(
            "UPDATE knowledge_items SET disabled_at = ? WHERE knowledge_id = ?",
            (stamp, knowledge_id),
        )


def _set_valid_until(ledger: Ledger, knowledge_id: str, *, seconds_ago: int | None) -> None:
    stamp = "" if seconds_ago is None else (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    with ledger._connect() as connection:
        connection.execute(
            "UPDATE knowledge_items SET valid_until = ? WHERE knowledge_id = ?",
            (stamp, knowledge_id),
        )


class _FakeRagflowGcClient:
    def __init__(self, **kwargs):
        self.deleted: list[tuple[str, tuple[str, ...]]] = []
        self.chunks_body: list[str] = ["redacted session memory body line 1", "line 2"]
        self.fail_chunks: bool = False

    def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        self.deleted.append((dataset_id, tuple(document_ids)))

    def list_document_chunks(self, dataset_id: str, document_id: str, **kwargs) -> list[str]:
        if self.fail_chunks:
            raise RuntimeError("chunks fetch failed")
        return list(self.chunks_body)


def _sha(label: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    import hashlib

    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_memory(ledger: Ledger, *, knowledge_id: str, document_id: str, session_id_hash: str = SESSION_ID_HASH):
    source_content_hash = _sha(f"{knowledge_id}:source")
    source_window_hash = _sha(f"{knowledge_id}:window")
    item = ledger.upsert_session_memory(
        knowledge_id=knowledge_id,
        content_hash=_sha(knowledge_id),
        provider="codex",
        project=PROJECT,
        session_id_hash=session_id_hash,
        title=knowledge_id,
        summary=knowledge_id,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_edge_manifest_hash([(source_content_hash, source_window_hash)]),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source_content_hash,
        source_window_hash=source_window_hash,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds_session_memory", document_id=document_id, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    return ledger.get_by_knowledge_id(item["knowledge_id"])


def test_session_memory_gc_deletes_disabled_row_only_after_replacement_active(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old", document_id="doc_gc_old")
    active = _session_memory(ledger, knowledge_id="kn_gc_active", document_id="doc_gc_active")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    dry = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=False,
        ),
        token="test-token",
    ).run()
    executed = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()
    repeated = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert dry["eligible_count"] == 1
    assert dry["mutation_performed"] is False
    assert executed["deleted_count"] == 1
    assert fake.deleted == [("ds_session_memory", ("doc_gc_old",))]
    tombstone = json.loads(Ledger(ledger_path).get_by_knowledge_id(old["knowledge_id"])["metadata_json"])
    assert tombstone["session_memory_gc"]["status"] == "deleted"
    assert repeated["eligible_count"] == 0
    assert repeated["deleted_count"] == 0


def test_session_memory_gc_requires_promoted_dirty_and_active_replacement(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_no_replacement", document_id="doc_gc_old_no_replacement")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 0
    assert fake.deleted == []


def test_session_memory_gc_floors_min_disabled_age_to_block_fresh_disable(tmp_path, monkeypatch):
    # G-1 (M-GC §6): a freshly disabled session_memory must NOT be hard-deletable,
    # even when the caller passes min_disabled_age_seconds=0. The floor is enforced
    # in code (max with MIN_DISABLED_AGE_FLOOR_SECONDS), not just via a default.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_fresh", document_id="doc_gc_old_fresh")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_fresh", document_id="doc_gc_active_fresh")
    ledger.mark_disabled(old["knowledge_id"])  # disabled_at = now (fresh)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    blocked = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            min_disabled_age_seconds=0,  # caller attempts to bypass the floor
            execute=True,
        ),
        token="test-token",
    ).run()

    assert blocked["eligible_count"] == 0
    assert blocked["deleted_count"] == 0
    assert fake.deleted == []
    assert blocked["min_disabled_age_floor_seconds"] == MIN_DISABLED_AGE_FLOOR_SECONDS
    assert blocked["effective_min_disabled_age_seconds"] == MIN_DISABLED_AGE_FLOOR_SECONDS

    # Once the disable is older than the floor, the same row becomes eligible.
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    aged = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            min_disabled_age_seconds=0,
            execute=True,
        ),
        token="test-token",
    ).run()

    assert aged["eligible_count"] == 1
    assert aged["deleted_count"] == 1
    assert fake.deleted == [("ds_session_memory", ("doc_gc_old_fresh",))]


def test_session_memory_gc_requires_authorized_replacement_not_just_active_flag(tmp_path, monkeypatch):
    # G-2 (M-GC §6): the replacement must pass authorize_document, not just the
    # candidate SQL column flags. An expired active snapshot still passes the
    # EXISTS column checks (indexed / auth-active / has-doc / dataset) but fails
    # authorize_document, so the superseded old must NOT be hard-deleted.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_authz", document_id="doc_gc_old_authz")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_authz", document_id="doc_gc_active_authz")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    _set_valid_until(ledger, active["knowledge_id"], seconds_ago=3600)  # replacement expired
    blocked = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()
    assert blocked["eligible_count"] == 0
    assert blocked["deleted_count"] == 0
    assert fake.deleted == []

    _set_valid_until(ledger, active["knowledge_id"], seconds_ago=None)  # clear expiry -> authorized
    ok = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()
    assert ok["eligible_count"] == 1
    assert ok["deleted_count"] == 1
    assert fake.deleted == [("ds_session_memory", ("doc_gc_old_authz",))]


def test_session_memory_gc_revalidates_each_row_before_delete(tmp_path, monkeypatch):
    # G-4 (M-GC §3.3 E2a / §6): intra-run TOCTOU guard. run() reads the candidate
    # list once, then deletes row by row. A prior row's delete_documents network
    # call can take time, during which a concurrent writer may rotate the active
    # snapshot (e.g. roll the replacement back to disabled), invalidating a row
    # still selected from the stale list. Such a row MUST be re-validated
    # immediately before its delete and SKIPPED (no mutation), not hard-deleted.
    #
    # Two superseded candidates share ONE active replacement in the SAME session.
    # The fake disables that replacement on its first delete_documents call,
    # simulating mid-run snapshot rotation: the second candidate no longer has an
    # authorized replacement, so re-validation must skip it.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old_a = _session_memory(ledger, knowledge_id="kn_gc_old_a", document_id="doc_gc_old_a")
    old_b = _session_memory(ledger, knowledge_id="kn_gc_old_b", document_id="doc_gc_old_b")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_shared", document_id="doc_gc_active_shared")
    for old in (old_a, old_b):
        ledger.mark_disabled(old["knowledge_id"])
        _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )

    active_knowledge_id = active["knowledge_id"]

    class _RotatingFakeRagflowGcClient(_FakeRagflowGcClient):
        def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
            # 첫 delete 직후 active replacement를 disable -> snapshot rotation 시뮬레이션
            first = not self.deleted
            super().delete_documents(dataset_id, document_ids)
            if first:
                Ledger(ledger_path).mark_disabled(active_knowledge_id)

    fake = _RotatingFakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert report["eligible_count"] == 2
    assert report["deleted_count"] == 1
    assert report["revalidation_skipped_count"] >= 1
    # 두 번째 후보(snapshot rotation 이후)는 hard delete되지 않는다.
    assert len(fake.deleted) == 1
    assert fake.deleted[0][0] == "ds_session_memory"
    deleted_doc_ids = fake.deleted[0][1]
    assert deleted_doc_ids == ("doc_gc_old_a",) or deleted_doc_ids == ("doc_gc_old_b",)
    # 정확히 하나만 tombstone 처리된다.
    final = Ledger(ledger_path)
    tombstoned = [
        kid
        for kid in (old_a["knowledge_id"], old_b["knowledge_id"])
        if json.loads(final.get_by_knowledge_id(kid)["metadata_json"]).get("session_memory_gc", {}).get("status")
        == "deleted"
    ]
    assert len(tombstoned) == 1


def test_session_memory_gc_refuses_disallowed_retention_policy(tmp_path, monkeypatch):
    # G-5 (M-GC §3.5 T1 / §6): session-memory-gc는 retention policy가
    # 'supersede_or_disable'인 dataset에만 동작한다. 선언된 policy/role이 허용 집합
    # 밖이면(예: transcript-memory의 'private_indefinite_until_disabled') 어떤
    # mutation도 하기 전에 거부한다. offline에서는 dataset_id->policy 매핑이 없으므로
    # 선언된 role/policy로 강제한다.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_policy", document_id="doc_gc_old_policy")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_policy", document_id="doc_gc_active_policy")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    # transcript-memory의 정책을 선언 -> session GC 허용 집합 밖이므로 거부.
    blocked = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            declared_retention_policy="private_indefinite_until_disabled",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert blocked["status"] == "blocked_retention_policy"
    assert blocked["retention_policy_enforced"] is True
    assert blocked["eligible_count"] == 0
    assert blocked["selected_count"] == 0
    assert blocked["deleted_count"] == 0
    assert blocked["mutation_performed"] is False
    assert blocked["network_used"] is False
    assert fake.deleted == []


def test_session_memory_gc_allows_declared_supersede_policy(tmp_path, monkeypatch):
    # G-5: 허용된 policy(또는 그 role/name)를 선언하면 정상 진행하고
    # retention_policy_enforced=true를 기록한다.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_allow", document_id="doc_gc_old_allow")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_allow", document_id="doc_gc_active_allow")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    ok = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            declared_dataset_role="session_memory",  # role -> supersede_or_disable
            execute=True,
        ),
        token="test-token",
    ).run()

    assert ok["status"] == "ok"
    assert ok["retention_policy_enforced"] is True
    assert ok["eligible_count"] == 1
    assert ok["deleted_count"] == 1
    assert fake.deleted == [("ds_session_memory", ("doc_gc_old_allow",))]


def test_session_memory_gc_absent_policy_keeps_prior_behavior(tmp_path, monkeypatch):
    # G-5: declaration이 없으면(dataset_id-only caller) 기존 동작을 유지하고
    # retention_policy_enforced=false만 추가로 기록한다.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_absent", document_id="doc_gc_old_absent")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_absent", document_id="doc_gc_active_absent")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()

    assert report["status"] == "ok"
    assert report["retention_policy_enforced"] is False
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 1
    assert fake.deleted == [("ds_session_memory", ("doc_gc_old_absent",))]


def test_session_memory_gc_writes_durable_audit_record(tmp_path, monkeypatch):
    # G-3 (M-GC §6, A1/A2/A3): every successful hard delete writes exactly one
    # append-only audit row. Because the RAGFlow doc is gone after a hard delete,
    # the audit must carry the replacement active_knowledge_id (A2) and the bound
    # epoch markers (E3), and must store only the sha256 hash of the doc id (A3).
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old = _session_memory(ledger, knowledge_id="kn_gc_old_audit", document_id="doc_gc_old_audit")
    active = _session_memory(ledger, knowledge_id="kn_gc_active_audit", document_id="doc_gc_active_audit")
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(
        session_id_hash=SESSION_ID_HASH,
        provider="codex",
        project=PROJECT,
        reason="gc-test",
    )
    ledger.mark_dirty_session_memory_promoted(
        session_id_hash=SESSION_ID_HASH,
        summary_knowledge_id=active["knowledge_id"],
    )
    fake = _FakeRagflowGcClient()
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            execute=True,
        ),
        token="test-token",
    ).run()
    assert report["deleted_count"] == 1
    assert report["raw_ids_printed"] is False

    audit_rows = Ledger(ledger_path).list_memory_gc_audit()
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit["gc_kind"] == "session_memory"
    assert audit["operation"] == gc_module.SESSION_MEMORY_GC_OPERATION
    assert audit["schema_version"] == gc_module.SESSION_MEMORY_GC_SCHEMA_VERSION
    assert audit["mode"] == "execute"
    assert audit["knowledge_id"] == old["knowledge_id"]
    assert audit["dataset_id"] == "ds_session_memory"
    assert audit["mutated"] == 1
    assert audit["audit_id"]
    assert audit["created_at"]
    # A2: the replacement that justified the irreversible delete is reconstructable.
    assert audit["replacement_knowledge_id"] == active["knowledge_id"]
    # E3: bound epoch markers are present.
    assert audit["dirty_at"]
    assert audit["snapshot_updated_at"]
    assert audit["age_gate_seconds"] == MIN_DISABLED_AGE_FLOOR_SECONDS
    # A3: only the sha256 hash of the raw doc id is stored, never the raw id.
    expected_hash = hashlib.sha256(b"doc_gc_old_audit").hexdigest()
    assert audit["ragflow_document_id_hash"] == expected_hash
    assert len(audit["ragflow_document_id_hash"]) == 64
    assert audit["ragflow_document_id_hash"] != "doc_gc_old_audit"
    assert "doc_gc_old_audit" not in json.dumps(audit)


class _BombRagflowGcClient:
    # 생성되기만 해도 실패 -> retention gate가 client 생성 *전에* 거부함을 증명한다.
    def __init__(self, **kwargs):
        raise AssertionError("RagflowHttpClient must not be constructed when retention policy is blocked")


def test_session_memory_gc_cli_blocks_disallowed_retention_policy(tmp_path, monkeypatch, capsys):
    # G-5 CLI (M-GC §3.5 T1 / §6): main() argv 레벨에서 --declared-retention-policy가
    # 허용 집합 밖이면 client 생성 전에 거부하고 nonzero exit + blocked_retention_policy를
    # 보고한다. BombClient로 client 미생성을 강제 증명한다.
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)  # ensure schema exists
    monkeypatch.setattr(gc_module, "RagflowHttpClient", _BombRagflowGcClient)

    exit_code = gc_module.main(
        [
            "--ledger",
            str(ledger_path),
            "--dataset-id",
            "ds_session_memory",
            "--ragflow-url",
            "http://localhost:9380",
            "--declared-retention-policy",
            "private_indefinite_until_disabled",  # transcript policy -> disallowed for session GC
        ]
    )

    out = capsys.readouterr().out
    report = json.loads(out.strip().splitlines()[-1])
    assert exit_code != 0
    assert report["status"] == "blocked_retention_policy"
    assert report["retention_policy_enforced"] is True
    assert report["eligible_count"] == 0
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_session_memory_gc_cli_allows_declared_role_dry_run(tmp_path, monkeypatch, capsys):
    # G-5 CLI: 허용된 role을 --declared-dataset-role로 선언하면 dry-run이 정상 진행되고
    # retention_policy_enforced=true를 보고한다. --execute 미설정이므로 client는 생성되지
    # 않는다(BombClient가 생성되면 실패).
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)
    monkeypatch.setattr(gc_module, "RagflowHttpClient", _BombRagflowGcClient)

    exit_code = gc_module.main(
        [
            "--ledger",
            str(ledger_path),
            "--dataset-id",
            "ds_session_memory",
            "--ragflow-url",
            "http://localhost:9380",
            "--declared-dataset-role",
            "session_memory",  # -> supersede_or_disable (allowed)
        ]
    )

    out = capsys.readouterr().out
    report = json.loads(out.strip().splitlines()[-1])
    assert exit_code == 0
    assert report["status"] == "ok"
    assert report["mode"] == "dry_run"
    assert report["retention_policy_enforced"] is True
    assert report["network_used"] is False


def _bk_eligible_setup(ledger, *, old_kid, old_doc, active_kid, active_doc, now: datetime | None = None):
    # ``now``: runner에 frozen clock을 주입하는 테스트용 기준 시각. disabled_at backdate를
    # 같은 기준으로 맞춰 age gate가 실행 날짜와 무관하게 결정적이게 한다(stale fixture 방지).
    old = _session_memory(ledger, knowledge_id=old_kid, document_id=old_doc)
    active = _session_memory(ledger, knowledge_id=active_kid, document_id=active_doc)
    ledger.mark_disabled(old["knowledge_id"])
    _backdate_disabled_at(ledger, old["knowledge_id"], seconds_ago=2 * MIN_DISABLED_AGE_FLOOR_SECONDS, now=now)
    ledger.promote_session_memory(active["knowledge_id"])
    ledger.mark_session_memory_dirty(session_id_hash=SESSION_ID_HASH, provider="codex", project=PROJECT, reason="bk")
    ledger.mark_dirty_session_memory_promoted(session_id_hash=SESSION_ID_HASH, summary_knowledge_id=active["knowledge_id"])
    return old, active


def test_session_memory_gc_backs_up_before_delete(tmp_path, monkeypatch):
    # G-8 (recoverable delete): execute backs up the doc body + recovery meta to a
    # private store BEFORE the irreversible delete; raw doc id is never persisted.
    from agent_knowledge.session_memory.gc_backup import list_gc_backups, read_gc_backup

    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old, active = _bk_eligible_setup(ledger, old_kid="kn_bk_old", old_doc="doc_bk_old_RAW", active_kid="kn_bk_active", active_doc="doc_bk_active")
    fake = _FakeRagflowGcClient()
    fake.chunks_body = ["redacted body A", "redacted body B"]
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)
    bk = tmp_path / "gc-backup"

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            backup_dir=str(bk),
            execute=True,
        ),
        token="test-token",
    ).run()

    assert report["deleted_count"] == 1
    assert report["backed_up_count"] == 1
    assert report["backup_enabled"] is True
    assert fake.deleted == [("ds_session_memory", ("doc_bk_old_RAW",))]
    backups = list_gc_backups(bk, kind="session_memory")
    assert len(backups) == 1
    rec = read_gc_backup(backups[0])
    assert rec["body"] == "redacted body A\nredacted body B"
    assert rec["replacement_knowledge_id"] == active["knowledge_id"]
    assert "doc_bk_old_RAW" not in backups[0].read_text(encoding="utf-8")


def test_session_memory_gc_backup_failure_aborts_delete(tmp_path, monkeypatch):
    # G-8: if the pre-delete backup fails, the irreversible delete MUST NOT happen.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _bk_eligible_setup(ledger, old_kid="kn_bkf_old", old_doc="doc_bkf_old", active_kid="kn_bkf_active", active_doc="doc_bkf_active")
    fake = _FakeRagflowGcClient()
    fake.fail_chunks = True  # backup body fetch fails
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            backup_dir=str(tmp_path / "gc-backup"),
            execute=True,
        ),
        token="test-token",
    ).run()

    assert fake.deleted == []  # no delete without a successful backup
    assert report["deleted_count"] == 0
    assert report["backed_up_count"] == 0
    assert report["status"] == "partial_failed"


def test_session_memory_gc_empty_body_aborts_delete(tmp_path, monkeypatch):
    # G-8: an empty body backup would be lossy (unrecoverable) -> delete must abort.
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _bk_eligible_setup(ledger, old_kid="kn_eb_old", old_doc="doc_eb_old", active_kid="kn_eb_active", active_doc="doc_eb_active")
    fake = _FakeRagflowGcClient()
    fake.chunks_body = []  # document has no parsed chunks -> empty body
    monkeypatch.setattr(gc_module, "RagflowHttpClient", lambda **kwargs: fake)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            backup_dir=str(tmp_path / "gc-backup"),
            execute=True,
        ),
        token="test-token",
    ).run()

    assert fake.deleted == []
    assert report["deleted_count"] == 0
    assert report["backed_up_count"] == 0
    assert report["status"] == "partial_failed"


class _RecordingGcClient:
    """S0a 주입 seam용 recording client: 모든 호출을 순서대로 기록해 비가역 경로의
    부수효과 시퀀스(backup→delete)를 결정적으로 단언한다. delete는 success를 반환하므로
    tombstone+audit 경로가 실제 실행된다(vacuous-green 방지)."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.chunks_body = ["redacted body A", "redacted body B"]

    def list_document_chunks(self, dataset_id, document_id, **kwargs):
        self.calls.append(("list_document_chunks", (dataset_id, document_id)))
        return list(self.chunks_body)

    def delete_documents(self, dataset_id, document_ids):
        self.calls.append(("delete_documents", (dataset_id, tuple(document_ids))))


def test_session_memory_gc_characterization_trace_frozen(tmp_path):
    # S0a/S1 특성화 baseline (게이트의 non-vacuous A1 오라클): 주입 seam(recording
    # client + frozen clock)으로 비가역 경로의 순서화된 부수효과 + audit 행 형태를
    # 고정한다. S2/S3가 _mark_gc_deleted/audit를 seam 뒤로 옮긴 뒤에도 이 트레이스가
    # (volatile audit_id/created_at 제외) byte-identical해야 한다.
    frozen = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    old, active = _bk_eligible_setup(
        ledger, old_kid="kn_ch_old", old_doc="doc_ch_old", active_kid="kn_ch_active", active_doc="doc_ch_active", now=frozen
    )
    rec = _RecordingGcClient()

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            backup_dir=str(tmp_path / "gc-backup"),
            execute=True,
        ),
        token="test-token",
        ragflow_client=rec,
        now_fn=lambda: frozen,
    ).run()

    # 1) 순서화된 부수효과: backup(list_document_chunks) → delete
    assert rec.calls == [
        ("list_document_chunks", ("ds_session_memory", "doc_ch_old")),
        ("delete_documents", ("ds_session_memory", ("doc_ch_old",))),
    ]
    assert report["deleted_count"] == 1
    # 2) tombstone: deleted_at == frozen (now_fn 주입 증명) + status deleted
    tombstone = json.loads(
        Ledger(ledger_path).get_by_knowledge_id(old["knowledge_id"])["metadata_json"]
    )["session_memory_gc"]
    assert tombstone["status"] == "deleted"
    assert tombstone["deleted_at"] == frozen.isoformat()
    # 3) audit: 1 row, deterministic 필드 고정 / volatile은 존재만 / raw doc id 미저장
    audits = Ledger(ledger_path).list_memory_gc_audit()
    assert len(audits) == 1
    a = audits[0]
    deterministic = {
        k: a[k]
        for k in (
            "gc_kind", "operation", "schema_version", "mode", "knowledge_id",
            "dataset_id", "replacement_knowledge_id", "approval_operation",
            "age_gate_seconds", "mutated",
        )
    }
    assert deterministic == {
        "gc_kind": "session_memory",
        "operation": gc_module.SESSION_MEMORY_GC_OPERATION,
        "schema_version": gc_module.SESSION_MEMORY_GC_SCHEMA_VERSION,
        "mode": "execute",
        "knowledge_id": old["knowledge_id"],
        "dataset_id": "ds_session_memory",
        "replacement_knowledge_id": active["knowledge_id"],
        "approval_operation": gc_module.SESSION_MEMORY_GC_OPERATION,
        "age_gate_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
        "mutated": 1,
    }
    assert a["dirty_at"] and a["snapshot_updated_at"]  # bound epoch markers wired
    assert a["audit_id"] and a["created_at"]  # volatile: presence only
    assert a["ragflow_document_id_hash"] and "doc_ch_old" not in a["ragflow_document_id_hash"]


def test_session_memory_gc_orphan_delete_when_audit_raises(tmp_path, monkeypatch):
    # A2 orphan-injection 오라클: delete 성공 *후* audit가 raise하면 RAGFlow doc은 이미
    # 삭제됐는데(비가역) audit row가 없는 orphan 상태가 된다. 이 partial-failure 동작을
    # 특성화로 고정한다(seam 라우팅이 순서/실패경로를 바꾸면 이 테스트가 깨진다).
    frozen = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _bk_eligible_setup(
        ledger, old_kid="kn_orph_old", old_doc="doc_orph_old", active_kid="kn_orph_active", active_doc="doc_orph_active", now=frozen
    )
    rec = _RecordingGcClient()

    def _raise_audit(self, ctx):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(gc_module.LedgerGCSafetyAuditor, "record_gc_audit", _raise_audit)

    report = SessionMemoryGcRunner(
        config=SessionMemoryGcConfig(
            ledger_path=ledger_path,
            dataset_id="ds_session_memory",
            ragflow_url="http://localhost:9380",
            backup_dir=str(tmp_path / "gc-backup"),
            execute=True,
        ),
        token="test-token",
        ragflow_client=rec,
        now_fn=lambda: frozen,
    ).run()

    # 비가역 delete는 이미 일어났다(orphan) — backup → delete까지 갔고 audit에서 터짐
    assert ("delete_documents", ("ds_session_memory", ("doc_orph_old",))) in rec.calls
    assert report["failed_count"] == 1
    assert report["deleted_count"] == 0  # audit 전 실패라 deleted 카운트되지 않음
    assert report["status"] == "partial_failed"
    # audit row 없음 = orphan delete (이 위험이 코드에 존재함을 명시 고정)
    assert Ledger(ledger_path).list_memory_gc_audit() == []
