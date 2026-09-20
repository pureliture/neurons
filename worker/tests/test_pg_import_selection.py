"""명시적 범위 포인트 선택(explicit bounded point selection) 단위 및 통합 테스트."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from test_pg_qdrant_import_cli import (
    Boundary,
    ReadOnlySQL,
    lane,
    cli,
    invoke,
    approve,
    record_imports,
)


def make_selection_file(path: Path, ids: list, name: str = "point_ids.json") -> Path:
    f = path / name
    f.write_text(json.dumps(ids))
    return f


def setup_multi_points(boundary: Boundary, count: int = 4):
    """id 101부터 count개의 고유 session memory point를 생성하여 boundary에 배치."""
    base = boundary.points[0]
    points = []
    for i in range(count):
        pid = 101 + i
        p = deepcopy(base)
        p["id"] = pid
        points.append(p)
    boundary.points = points
    return points


def test_feature_missing_red_cli_rejects_or_parses_selection_file(lane, capsys):
    """기능이 없을 때는 --point-ids-file 인자가 거부되어야 함 (RED)."""
    boundary, argv, path = lane
    sel_file = make_selection_file(path, [1])
    extra = ["--point-ids-file", str(sel_file)]
    rc, report = invoke(lane, capsys, extra=extra)
    assert rc == 0
    assert report["status"] == "dry_run"
    assert report["selected_count"] == 1


def test_two_disjoint_explicit_batches_retrieve_different_points_not_prefix(lane, capsys):
    """두 개의 서로소 명시적 배치가 scroll prefix가 아닌 서로 다른 포인트를 retrieve하는지 검증."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=4)
    # boundary.points에는 id 101, 102, 103, 104가 있음.
    # batch 1: [101, 102]
    sel1 = make_selection_file(path, [101, 102], name="sel1.json")
    manifest1 = path / "manifest1.json"
    argv1 = list(argv)
    argv1[argv1.index("--manifest") + 1] = str(manifest1)
    boundary.events.clear()

    rc1, report1 = invoke((boundary, argv1, path), capsys, extra=["--point-ids-file", str(sel1)])
    assert rc1 == 0
    assert report1["status"] == "dry_run"
    assert report1["selected_count"] == 2
    # scroll 호출이 전혀 없어야 하고 retrieve만 사용되어야 함
    scroll_events1 = [e for e in boundary.events if e[0] == "scroll"]
    assert len(scroll_events1) == 0, "explicit mode must not call scroll"
    retrieve_events1 = [e for e in boundary.events if e[0] == "retrieve"]
    assert len(retrieve_events1) == 2  # page-size is 1, so 2 calls
    retrieved_ids1 = [e[1][0] for e in retrieve_events1]
    assert retrieved_ids1 == [101, 102]

    # batch 2: [103, 104] (접두어가 아님!)
    sel2 = make_selection_file(path, [103, 104], name="sel2.json")
    manifest2 = path / "manifest2.json"
    argv2 = list(argv)
    argv2[argv2.index("--manifest") + 1] = str(manifest2)
    boundary.events.clear()

    rc2, report2 = invoke((boundary, argv2, path), capsys, extra=["--point-ids-file", str(sel2)])
    assert rc2 == 0
    assert report2["status"] == "dry_run"
    assert report2["selected_count"] == 2
    scroll_events2 = [e for e in boundary.events if e[0] == "scroll"]
    assert len(scroll_events2) == 0, "explicit mode must not call scroll"
    retrieve_events2 = [e for e in boundary.events if e[0] == "retrieve"]
    assert len(retrieve_events2) == 2
    retrieved_ids2 = [e[1][0] for e in retrieve_events2]
    assert retrieved_ids2 == [103, 104]

    # batch 1과 batch 2가 완전히 서로 다른 포인트를 가져왔는지 확인
    assert set(retrieved_ids1).isdisjoint(set(retrieved_ids2))


def test_dryrun_remote_write0(lane, capsys):
    """explicit-ID mode dry-run 시 remote write가 0이고 SQL 쓰기 시도가 없어야 함."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=2)
    sel = make_selection_file(path, [101, 102])
    rc, report = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    assert rc == 0
    assert report["status"] == "dry_run"
    assert report["mutation_started"] is False
    assert boundary.sql.writes == 0


def test_only_approved_id_order_fetched(lane, capsys):
    """선택 파일에 명시된 순서대로 retrieve되고 manifest에 보존되어야 함."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=3)
    # 순서를 뒤섞음: [103, 101]
    sel = make_selection_file(path, [103, 101])
    boundary.events.clear()
    rc, report = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    assert rc == 0
    retrieves = [e for e in boundary.events if e[0] == "retrieve"]
    assert [e[1][0] for e in retrieves] == [103, 101]
    manifest = json.loads((path / "manifest.json").read_text())
    assert [p["id"] for p in manifest["points"]] == [103, 101]


@pytest.mark.parametrize("fault", [
    "wrongscope_project",
    "wrongscope_provider",
    "stale",
    "unknown_id",
    "duplicate_id",
    "empty_list",
    "not_a_list",
    "oversized_list",
    "oversized_file",
    "invalid_point_id_type",
    "invalid_uuid_string",
    "missing_file",
])
def test_wrongscope_stale_unknown_duplicate_tampered_file_reject(lane, capsys, fault):
    """scope 불일치, stale, 알 수 없는 ID, 중복 ID, 잘못된 파일 형식 등 모두 fail closed reject."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=4)

    if fault == "wrongscope_project":
        boundary.points[0]["payload"]["project"] = "other-project"
        sel = make_selection_file(path, [101])
    elif fault == "wrongscope_provider":
        boundary.points[0]["payload"]["provider"] = "other-provider"
        sel = make_selection_file(path, [101])
    elif fault == "stale":
        boundary.points[0]["payload"]["source_hash"] = "stale-hash"
        sel = make_selection_file(path, [101])
    elif fault == "unknown_id":
        sel = make_selection_file(path, [9999])  # boundary에 존재하지 않음
    elif fault == "duplicate_id":
        sel = make_selection_file(path, [101, 101])
    elif fault == "empty_list":
        sel = make_selection_file(path, [])
    elif fault == "not_a_list":
        f = path / "not_a_list.json"
        f.write_text(json.dumps({"id": 101}))
        sel = f
    elif fault == "oversized_list":
        # limit는 2인데 3개 전달
        sel = make_selection_file(path, [101, 102, 103])
    elif fault == "oversized_file":
        f = path / "oversized.json"
        # 16MB 초과 파일 생성 시뮬레이션
        f.write_bytes(b"[" + b"1," * (8 * 1024 * 1024) + b"1]")
        sel = f
    elif fault == "invalid_point_id_type":
        sel = make_selection_file(path, [101, 3.14])
    elif fault == "invalid_uuid_string":
        sel = make_selection_file(path, [101, "not-a-valid-uuid"])
    elif fault == "missing_file":
        sel = path / "nonexistent.json"

    rc, report = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    if fault == "stale":
        # stale 포인트는 dry-run manifest에 기록되나 complete=False, selected_batch_complete=False -> rc=2
        assert rc == 2
        manifest = json.loads((path / "manifest.json").read_text())
        assert manifest["points"][0]["status"] == "stale"
        assert manifest["selected_batch_complete"] is False
    else:
        # 나머지는 즉시 거부 (status=rejected, rc!=0, writes=0)
        assert rc != 0
        assert report["status"] == "rejected"
        assert boundary.sql.writes == 0


def test_exact_apply_manifest_binding(lane, capsys, monkeypatch):
    """dry-run manifest의 selection digest와 apply 시점의 selection file이 정확히 일치해야 하며 변조 시 reject."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=2)
    sel = make_selection_file(path, [101, 102])
    argv_with_sel = argv + ["--point-ids-file", str(sel)]

    # dry-run 실행
    rc_dry, report_dry = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    assert rc_dry == 0
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["selection_mode"] == "explicit"
    assert "point_ids_digest" in manifest

    # 승인 생성
    approval = {
        "schema_version": 1,
        "operation": "pg-qdrant-import",
        "approved": True,
        "operator": "synthetic-human",
        "argv": argv_with_sel + ["--apply"],
        "plan_digest": manifest["plan_digest"],
    }
    (path / "approval.json").write_text(json.dumps(approval))

    # 변조 케이스 1: selection file 내용 변조 (ID 변경)
    sel.write_text(json.dumps([101, 103]))
    writes = record_imports(monkeypatch, boundary)
    rc_tampered, report_tampered = invoke(
        (boundary, argv_with_sel, path), capsys, extra=["--apply"]
    )
    assert rc_tampered != 0
    assert report_tampered["status"] == "rejected"
    assert writes == []

    # 변조 케이스 2: selection file 내용 복원 후 정상 apply
    sel.write_text(json.dumps([101, 102]))
    boundary.events.clear()
    rc_apply, report_apply = invoke(
        (boundary, argv_with_sel, path), capsys, extra=["--apply"]
    )
    assert rc_apply == 0
    assert report_apply["status"] == "applied"
    assert writes == [101, 102]
    assert report_apply["selected_count"] == 2
    assert report_apply["complete"] is False  # corpus complete는 False!


def test_no_embeddings_leaked_in_manifest_or_report(lane, capsys):
    """manifest 및 보고서 출력에 raw vector나 임베딩 배열이 누출되지 않아야 함."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=2)
    sel = make_selection_file(path, [101, 102])
    rc, report = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    assert rc == 0
    manifest_text = (path / "manifest.json").read_text()
    assert '"vector"' not in manifest_text
    assert '"text"' not in manifest_text
    report_text = json.dumps(report)
    assert "vector" not in report_text
    assert "embedding" not in report_text


def test_selected_batch_success_does_not_assert_corpus_complete(lane, capsys):
    """선택된 배치가 성공하더라도 전체 코퍼스가 완료되었다고 주장(complete=True)하지 않아야 함."""
    boundary, argv, path = lane
    setup_multi_points(boundary, count=4)
    # 4개 중 2개만 선택
    sel = make_selection_file(path, [101, 102])
    rc, report = invoke(lane, capsys, extra=["--point-ids-file", str(sel)])
    assert rc == 0
    assert report["status"] == "dry_run"
    assert report["complete"] is False, "explicit batch success must NOT assert corpus complete"
    assert report["selected_batch_complete"] is True
    assert report["selected_count"] == 2

    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert manifest["selected_batch_complete"] is True
    assert manifest["selected_count"] == 2
