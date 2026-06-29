"""session-memory Qdrant searchable-mirror backfill CLI (CouchDB-native).

서브커맨드:
  verify            PROJECTED session-memory 세션 수를 센다(CouchDB read; 쓰기 없음).
  dry-run (default) 모든 mirror document를 materialize+build하되 upsert하지 않는다.
  run [--limit N]   mirror point를 upsert한다. 명시적 --collection이 필수다.
  rollback --submitted F   jsonl ``F``에 기록된 point만 정확히 삭제한다.
  parity            Parity-soak 엔트리포인트(RetiredIndexBridge primary vs CouchDB-joined mirror).

사용법(argparse 순서): GLOBAL 옵션(--collection / --checkpoint / --submitted /
--qdrant-url / --embedding-concurrency)은 서브커맨드 앞에 오고, 서브커맨드 전용
옵션(--limit / --create-collection)은 서브커맨드 뒤에 온다::

    qdrant-backfill --collection NAME --qdrant-url URL run --limit 10 --create-collection
    qdrant-backfill --collection NAME --submitted FILE rollback

source/authority: CouchDB source plane(go-forward recall authority; ledger
``knowledge_items``는 은퇴 중이며 여기서 사용하지 않는다). corpus는
``projection_state``가 ``projected``인 모든 세션이다.

안전장치:
- ``run``은 명시적 ``--collection``이 필수라서 live upsert가 default 이름으로
  흘러갈 수 없다(staging 이름은 허용되며, live 이름은 operator가 직접 입력할 때만 쓴다).
- ``run``/``rollback``은 ``ensure_collection=False``로 adapter를 만든다: 존재하지
  않는 target collection은 fail-closed이며(서버측에서 조용히 생성되지 않는다),
  ``run``은 최초 staging 셋업을 위해 ``--create-collection``을 넘길 수 있다.
- MIRROR-ONLY: CLI는 CouchDB primary나 RetiredIndexBridge에 쓰지 않고, dual-write backend도
  만들지 않는다. backfill은 CouchDB를 읽고(read-only materialize) Qdrant point만
  upsert/delete한다. RetiredIndexBridge는 parity의 primary fetch에서만 닿고 backfill에서는 닿지 않는다.
- 출력은 counts/statuses의 JSON이다(redaction-safe). jsonl audit / checkpoint는
  natural-key triple(content_hash + idempotency_key + target_profile)만 담고, body나
  raw id는 담지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

from .qdrant_backfill import (
    backfill_session_memory,
    iter_projected_session_memories,
    rollback_submitted,
)

# live mirror collection 이름(``run``에서는 operator가 직접 입력해야 한다).
LIVE_COLLECTION_NAME = "neurons_mirror_gemini_3072_v1"


# --------------------------------------------------------------------------- IO

def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _load_submitted_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"submitted jsonl not found: {file_path.name}")
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 손상/부분 라인(예: 중단된 append)은 manifest 전체에 대한 rollback을
            # 중단시키지 않고 관용적으로 건너뛴다. checkpoint loader의 관용성과 동일.
            continue
    return records


def _load_checkpoint_hashes(path: str | None) -> set[str]:
    if not path:
        return set()
    file_path = Path(path)
    if not file_path.exists():
        return set()
    hashes: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        content_hash = str(record.get("content_hash") or "")
        if content_hash:
            hashes.add(content_hash)
    return hashes


def _appender(*paths: str | None):
    """각 triple을 jsonl 라인으로 주어진 모든 경로에 append하는 on_submit을 반환한다.

    natural-key triple만 담으므로(body/raw-id 없음) jsonl은 rollback manifest이자
    resume checkpoint 역할을 겸한다.
    """
    targets = [Path(p) for p in paths if p]
    handles = [p.open("a", encoding="utf-8") for p in targets]

    def _on_submit(triple: dict[str, Any]) -> None:
        line = json.dumps(triple, ensure_ascii=False, sort_keys=True)
        for handle in handles:
            handle.write(line + "\n")
            handle.flush()

    _on_submit.close = lambda: [handle.close() for handle in handles]  # type: ignore[attr-defined]
    return _on_submit


# ----------------------------------------------------------------- wiring (live)

def _build_store(args):
    """env에서 CouchDB source store를 만든다(backfill에서는 read-only로만 사용).

    ``couchdb_source.build_cli``의 env 계약을 따른다: COUCHDB_URL(필수),
    COUCHDB_USER/COUCHDB_PASSWORD(basic auth), COUCHDB_DB(default
    ``transcript_source``). COUCHDB_URL이 없으면 fail-closed.
    """
    couchdb_url = os.environ.get("COUCHDB_URL", "")
    if not couchdb_url:
        raise SystemExit("CouchDB-native backfill에는 COUCHDB_URL이 필요하다")
    couchdb_user = os.environ.get("COUCHDB_USER", "")
    couchdb_password = os.environ.get("COUCHDB_PASSWORD", "")
    couchdb_db = os.environ.get("COUCHDB_DB", "transcript_source")
    auth_header = ""
    if couchdb_user:
        token = base64.b64encode(f"{couchdb_user}:{couchdb_password}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {token}"
    from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

    return CouchDBHttpSourceStore(base_url=couchdb_url, db=couchdb_db, auth_header=auth_header)


def _build_adapter(args, *, collection_name: str, ensure_collection: bool):
    """remote mirror adapter를 만든다.

    run/rollback에서는 ``ensure_collection``이 False다: 존재하지 않는 target
    collection은 서버측에서 조용히 생성되지 않고 fail-closed된다(해당 collection
    이름을 담은 SystemExit). 최초 생성을 원하면 ``run``에 ``--create-collection``을 넘긴다.
    """
    from .qdrant_docling_mirror import (
        PassthroughMarkdownNormalizer,
        build_remote_qdrant_docling_mirror_adapter,
    )
    from .qdrant_embedding import build_openai_embedding_provider

    # --qdrant-url가 QDRANT_URL env보다 우선한다(help 문구와 일치). 배포 러너는
    # QDRANT_URL env를 세팅하므로 env는 fallback으로 유지한다.
    url = str(args.qdrant_url or os.environ.get("QDRANT_URL") or "").strip()
    adapter = build_remote_qdrant_docling_mirror_adapter(
        url=url,
        collection_name=collection_name,
        embedding_provider=build_openai_embedding_provider(environ=os.environ),
        normalizer=PassthroughMarkdownNormalizer(),
        ensure_collection=ensure_collection,
    )
    if not ensure_collection:
        exists = adapter.collection_exists()
        if exists is False:
            raise SystemExit(
                f"target collection이 존재하지 않는다: {collection_name!r}. "
                "암묵적 생성을 거부한다. 새 collection을 만들려면 --create-collection"
                "(run 전용)을 넘기거나 --collection 이름을 고쳐라."
            )
    return adapter


# --------------------------------------------------------------------- commands

def _cmd_verify(args) -> int:
    store = _build_store(args)
    count = sum(1 for _ in iter_projected_session_memories(store))
    _write_json(
        {
            "command": "verify",
            "projected_session_memory_count": count,
            "network_used": False,
            "mutation_performed": False,
            "raw_ids_printed": False,
        }
    )
    return 0


def _cmd_dry_run(args) -> int:
    store = _build_store(args)
    report = backfill_session_memory(
        store=store,
        adapter=_DryRunAdapter(),
        dry_run=True,
        limit=args.limit,
    )
    out = report.to_dict()
    out["command"] = "dry-run"
    _write_json(out)
    return 0


def _cmd_run(args) -> int:
    if not args.collection:
        raise SystemExit("run에는 명시적 --collection이 필요하다(우발적 live write를 막기 위해 default를 거부)")
    store = _build_store(args)
    # 기본: collection을 절대 생성하지 않는다(오타는 fail-closed). --create-collection은
    # staging 셋업 한정으로 최초 생성을 opt-in한다.
    adapter = _build_adapter(
        args,
        collection_name=args.collection,
        ensure_collection=bool(getattr(args, "create_collection", False)),
    )
    already = _load_checkpoint_hashes(args.checkpoint)
    on_submit = _appender(args.submitted, args.checkpoint)
    try:
        report = backfill_session_memory(
            store=store,
            adapter=adapter,
            dry_run=False,
            limit=args.limit,
            on_submit=on_submit,
            already_submitted=already,
            concurrency=int(getattr(args, "embedding_concurrency", 1) or 1),
        )
    finally:
        close = getattr(on_submit, "close", None)
        if callable(close):
            close()
    out = report.to_dict()
    out["command"] = "run"
    out["collection"] = args.collection
    _write_json(out)
    return 0


def _cmd_rollback(args) -> int:
    if not args.submitted:
        raise SystemExit("rollback에는 --submitted <jsonl>이 필요하다")
    if not args.collection:
        raise SystemExit("rollback에는 명시적 --collection이 필요하다")
    # rollback은 collection을 절대 생성하지 않는다: 존재하지 않는 collection에서의
    # 삭제는 의미가 없고 오타는 반드시 fail-closed되어야 한다.
    adapter = _build_adapter(args, collection_name=args.collection, ensure_collection=False)
    submitted = _load_submitted_jsonl(args.submitted)
    report = rollback_submitted(adapter=adapter, submitted=submitted)
    out = report.to_dict()
    out["command"] = "rollback"
    out["collection"] = args.collection
    _write_json(out)
    return 0


def _cmd_parity(args) -> int:
    # parity soak는 RetiredIndexBridge baseline을 측정한 뒤 gate 시점에 설정되는 query cohort +
    # threshold, 그리고 live RetiredIndexBridge primary fetch와 mirror authority-join용 CouchDB
    # store가 필요하다. baseline을 CLI에 박아넣는 대신 wiring 엔트리포인트만 드러내고
    # 명시적 cohort 파일 없이는 verdict를 내지 않는다.
    if not args.cohort:
        raise SystemExit(
            "parity에는 --cohort <queries.txt>와 threshold 플래그가 필요하다. "
            "gate는 qdrant_backfill_parity.run_parity_soak를 통해 프로그램적으로 실행하라"
        )
    raise SystemExit(
        "parity CLI는 thin 엔트리포인트다. gate runner는 "
        "qdrant_backfill_parity.run_parity_soak에 있다(pure-compute, 주입 가능한 fetcher). "
        "primary_fetch = session-memory dataset에 대한 RetiredIndexBridge retrieve(이 CLI에서 "
        "RetiredIndexBridge를 쓰는 유일한 곳), mirror_fetch = CouchDB projection-state resolver로 "
        "join한 Qdrant query."
    )


class _DryRunAdapter:
    """dry-run용 adapter 대역: document를 검증하되 절대 쓰지 않는다.

    core에서 ``dry_run=True``는 ``submit_document``를 short-circuit하므로, 이 클래스는
    그 계약이 바뀌더라도 dry-run 코드 경로에서 write 경로에 도달할 수 없음을 보장하는
    안전망일 뿐이다.
    """

    embedding_size = None

    def collection_vector_size(self) -> None:
        return None

    def submit_document(self, *_args, **_kwargs):  # pragma: no cover - dry-run에서는 호출되지 않음
        raise AssertionError("dry-run은 document를 submit해서는 안 된다")


# ------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qdrant-backfill")
    parser.add_argument("--collection", default="", help="Qdrant collection 이름 (run/rollback에 필수)")
    parser.add_argument("--checkpoint", default="", help="jsonl resume checkpoint (hash만)")
    parser.add_argument("--submitted", default="", help="submit된 natural key의 jsonl manifest")
    parser.add_argument("--embedding-concurrency", type=int, default=1, help="embedding concurrency (default 1)")
    parser.add_argument("--qdrant-url", default="", help="Qdrant url (없으면 QDRANT_URL env를 fallback으로 사용)")
    parser.add_argument("--cohort", default="", help="parity: query cohort 파일")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("verify", help="PROJECTED session-memory 세션 수를 센다(CouchDB read; 쓰기 없음)")
    dry = sub.add_parser("dry-run", help="upsert 없이 materialize+build (default)")
    dry.add_argument("--limit", type=int, default=None)
    run = sub.add_parser("run", help="mirror point를 upsert한다(--collection 필수)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument(
        "--create-collection",
        action="store_true",
        help="없는 collection의 최초 생성을 허용한다(staging 셋업 전용; 기본 off)",
    )
    sub.add_parser("rollback", help="기록된 point를 삭제한다(--submitted 필수)")
    sub.add_parser("parity", help="parity soak 엔트리포인트")
    return parser


_DISPATCH = {
    "verify": _cmd_verify,
    "dry-run": _cmd_dry_run,
    "run": _cmd_run,
    "rollback": _cmd_rollback,
    "parity": _cmd_parity,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "dry-run"
    if not hasattr(args, "limit"):
        args.limit = None
    handler = _DISPATCH[command]
    return int(handler(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
