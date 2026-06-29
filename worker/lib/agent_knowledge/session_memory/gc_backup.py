"""Recoverable-delete backup store for GC hard deletes.

GC가 RetiredIndexBridge 문서를 hard delete(session_memory superseded 세대, 또는 covered
transcript chunk)하기 *전에* 그 문서의 본문(redacted MD)과 복구에 필요한 메타를
private(0700) JSON으로 보관한다. 삭제는 이로써 복구 가능해진다: 백업에서 본문을
RetiredIndexBridge에 재업로드 + 재임베딩(parse) + ledger row 복원.

redaction: raw RetiredIndexBridge document id는 저장하지 않고 sha256 hex만 남긴다(다른 GC
redaction 산출물과 정합, `raw_ids_printed: False` 불변과 같은 정책). body는 이미
redacted된 문서 본문이므로 live 문서와 동일 privacy 등급이며, 보관소는 0700으로 둔다.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

GC_BACKUP_SCHEMA_VERSION = "agent_knowledge_gc_backup.v1"
GC_BACKUP_KINDS = ("session_memory", "transcript_memory")


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(value or ""))
    return (cleaned or "unknown")[:120]


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def write_gc_backup(
    backup_dir: Path | str,
    *,
    kind: str,
    knowledge_id: str,
    content_hash: str,
    session_id_hash: str,
    provider: str,
    project: str,
    dataset_id: str,
    index_document_id: str,
    body: str,
    replacement_knowledge_id: str = "",
    coverage: list | None = None,
    extra: dict | None = None,
) -> Path:
    """삭제 전 복구용 백업 1건을 기록하고 그 경로를 돌려준다.

    저장 위치: ``<backup_dir>/<kind>/<safe(knowledge_id|content_hash)>.json``.
    원자적 write(tmp→replace) + 0700 디렉터리 / 0600 파일. raw document id 미저장.
    """
    if kind not in GC_BACKUP_KINDS:
        raise ValueError(f"unsupported gc backup kind: {kind!r}")
    root = Path(backup_dir)
    target_dir = root / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(target_dir, 0o700)
    record = {
        "schema_version": GC_BACKUP_SCHEMA_VERSION,
        "kind": kind,
        "knowledge_id": knowledge_id,
        "content_hash": content_hash,
        "session_id_hash": session_id_hash,
        "provider": provider,
        "project": project,
        "dataset_id": dataset_id,
        "index_document_id_hash": _sha256_hex(index_document_id),
        "body": body,
        "replacement_knowledge_id": replacement_knowledge_id,
        "coverage": coverage or [],
        "extra": extra or {},
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
    }
    path = target_dir / (_safe_name(knowledge_id or content_hash) + ".json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def read_gc_backup(path: Path | str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != GC_BACKUP_SCHEMA_VERSION:
        raise ValueError("not a valid gc backup record")
    return payload


def list_gc_backups(backup_dir: Path | str, *, kind: str | None = None) -> list[Path]:
    root = Path(backup_dir)
    kinds = (kind,) if kind else GC_BACKUP_KINDS
    out: list[Path] = []
    for k in kinds:
        d = root / k
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.json")))
    return out


def restore_gc_backup(retired_index_bridge, record: dict, *, dataset_id: str | None = None, parse: bool = True) -> dict:
    """복구: 백업 본문을 RetiredIndexBridge에 재업로드 + 재임베딩(``request_parse``)한다.

    GC된 콘텐츠를 다시 retrievable 상태로 되돌린다. RetiredIndexBridge document id는 재업로드 시
    새로 발급되므로(원래 id 부활 아님) 새 id를 돌려준다. ledger linkage 재구성은 별도
    단계(또는 정상 파이프라인 재적재)로 둔다 — 여기서는 "본문+임베딩 복원"까지 책임진다.
    """
    if not isinstance(record, dict) or record.get("schema_version") != GC_BACKUP_SCHEMA_VERSION:
        raise ValueError("not a valid gc backup record")
    ds = dataset_id or str(record.get("dataset_id") or "")
    if not ds:
        raise ValueError("dataset_id is required for restore")
    filename = "gc-restore-" + _safe_name(record.get("knowledge_id") or record.get("content_hash")) + ".md"
    uploaded = retired_index_bridge.upload_document(ds, str(record.get("body") or ""), filename=filename)
    new_document_id = uploaded.get("document_id") if isinstance(uploaded, dict) else None
    did_parse = False
    if parse and new_document_id:
        retired_index_bridge.request_parse(ds, [new_document_id])
        did_parse = True
    return {
        "restored": bool(new_document_id),
        "kind": record.get("kind"),
        "knowledge_id": record.get("knowledge_id"),
        "new_document_id": new_document_id,
        "parsed": did_parse,
    }


def main(argv: list[str] | None = None) -> int:
    """복구 CLI: 백업 1건을 RetiredIndexBridge에 재적재 + 재임베딩한다(additive, 삭제 아님)."""
    import argparse
    import json as _json
    import os
    import sys

    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="gc-restore")
    parser.add_argument("--backup", required=True, help="path to a gc backup json record")
    parser.add_argument("--retired-index-bridge-url", required=True)
    parser.add_argument("--dataset-id", default="", help="override target dataset id (default: record's)")
    parser.add_argument("--retired-index-bridge-token-env", default="RETIRED_INDEX_BRIDGE_API_KEY")
    parser.add_argument("--no-parse", action="store_true", help="skip re-embedding (request_parse)")
    ns = parser.parse_args(args)
    token = os.environ.get(ns.token_env, "")
    if not token:
        print("token env is not set", file=sys.stderr)
        return 2
    from ..index_client import RetiredIndexBridgeHttpClient

    record = read_gc_backup(ns.backup)
    retired_index_bridge = RetiredIndexBridgeHttpClient(base_url=ns.index_url, bearer_token=token, request_timeout_seconds=45)
    report = restore_gc_backup(retired_index_bridge, record, dataset_id=ns.dataset_id or None, parse=not ns.no_parse)
    report["raw_ids_printed"] = False
    safe = {k: v for k, v in report.items() if k != "new_document_id"}
    safe["new_document_id_present"] = bool(report.get("new_document_id"))
    print(_json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("restored") else 1
