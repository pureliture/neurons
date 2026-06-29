from __future__ import annotations

from datetime import datetime, timezone

from ..ledger import Ledger


DEFAULT_BRAIN_ID_PREFIX = "/project/"


def session_tag_for(statement_id: str) -> str:
    """statement_id -> RetiredIndexBridge join key. 단일 정의처(writer/recall 공유)."""
    return f"mem:{statement_id}"


def brain_id_for_project(project: str, *, prefix: str = DEFAULT_BRAIN_ID_PREFIX) -> str:
    """project -> brain_id 메타. write 어댑터와 recall-side project 필터의 단일 정의처
    (prefix drift 방지). 단일 운영 Memory 안에서 brain_id 로 project 를 식별/필터한다."""
    return f"{prefix}{project}"


class NativeMemoryMirrorStore:
    def __init__(self, ledger: Ledger):
        if ledger.read_only:
            raise ValueError("NativeMemoryMirrorStore requires a write-mode Ledger")
        self.ledger = ledger

    def _connect(self):
        return self.ledger._connect()

    def upsert_statement(
        self,
        *,
        statement_id: str,
        brain_id: str,
        original_content_hash: str,
        search_text: str = "",
        card_type: str = "",
        index_memory_id: str = "",
        now: datetime | None = None,
    ) -> dict:
        session_tag = session_tag_for(statement_id)
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO native_memory_mirror (
                    statement_id, brain_id, session_tag, status, superseded_by,
                    original_content_hash, search_text, card_type, index_memory_id,
                    index_disabled_at, created_at, superseded_at
                ) VALUES (?, ?, ?, 'active', '', ?, ?, ?, ?, '', ?, '')
                ON CONFLICT(statement_id) DO UPDATE SET
                    -- session_tag 은 INSERT 시 고정(=mem:<statement_id> 불변), SET 에서 의도적 제외.
                    -- created_at 은 의도적 제외: 최초 생성 시각 보존(빠진 컬럼 아님).
                    -- search_text/card_type 은 갱신(re-upsert 시 최신 텍스트로 reconcile stale-query 방지).
                    brain_id=excluded.brain_id,
                    original_content_hash=excluded.original_content_hash,
                    search_text=excluded.search_text,
                    card_type=excluded.card_type,
                    index_memory_id=excluded.index_memory_id,
                    status='active',
                    superseded_by='',
                    superseded_at=''
                """,
                (
                    statement_id,
                    brain_id,
                    session_tag,
                    original_content_hash,
                    search_text,
                    card_type,
                    index_memory_id,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM native_memory_mirror WHERE statement_id = ?",
                (statement_id,),
            ).fetchone()
        return dict(row)

    def mark_superseded(
        self,
        statement_id: str,
        *,
        superseded_by: str,
        now: datetime | None = None,
    ) -> bool:
        """active -> superseded 전이. status 전이가 실제로 일어났을 때만 True.

        `AND status='active'` 가드: 이미 superseded 인 row 를 재호출하면 rowcount=0 →
        False 를 반환하고 superseded_by / superseded_at 을 덮어쓰지 않는다. 후속 Option C
        reconciler 가 superseded_by 체인으로 disable 대상을 추적하므로 첫 superseder 정보를
        보존해야 한다(덮어쓰면 wrong target disable / row 고아화 위험).
        """
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE native_memory_mirror
                SET status='superseded', superseded_by=?, superseded_at=?
                WHERE statement_id=? AND status='active'
                """,
                (superseded_by, timestamp, statement_id),
            )
            return cursor.rowcount > 0

    def mark_index_disabled(
        self,
        statement_id: str,
        *,
        index_disabled_at: str,
        index_memory_id: str = "",
    ) -> bool:
        """superseded row 에 index_disabled_at 기록 + (비어있던 경우) index_memory_id backfill.

        멱등 가드: WHERE status='superseded' AND index_disabled_at='' → 이미 기록됐거나
        active 인 row 는 rowcount=0 → False. reconcile_one 이 그 session_tag 의 모든 message
        disable 확인 후에만 호출한다(부분실패 시 미호출 → row 가 list_pending_reconcile 에 잔존).
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE native_memory_mirror
                SET index_disabled_at=?,
                    index_memory_id = CASE
                        WHEN index_memory_id='' THEN ? ELSE index_memory_id END
                WHERE statement_id=? AND status='superseded' AND index_disabled_at=''
                """,
                (index_disabled_at, index_memory_id, statement_id),
            )
            return cursor.rowcount > 0

    def get_by_session_tags(self, session_tags: list[str]) -> dict[str, dict]:
        if not session_tags:
            return {}
        placeholders = ",".join("?" for _ in session_tags)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM native_memory_mirror WHERE session_tag IN ({placeholders})",
                list(session_tags),
            ).fetchall()
        return {row["session_tag"]: dict(row) for row in rows}

    def get_active_session_tags(self, brain_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_tag FROM native_memory_mirror "
                "WHERE brain_id = ? AND status = 'active'",
                (brain_id,),
            ).fetchall()
        return {row["session_tag"] for row in rows}

    def list_active_statements(self) -> list[dict]:
        """status='active' 행의 경량 뷰(statement_id/brain_id/hash). supersede-sync 입력.

        brain_id 무관 전체 active(단일 운영 Memory에 복수 brain_id 혼재 가능) → 각 행을
        ledger card state 와 대조해 ledger에서 은퇴한 orphan 을 찾는다.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT statement_id, brain_id, original_content_hash "
                "FROM native_memory_mirror WHERE status = 'active'",
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pending_reconcile(self, limit: int = 100) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM native_memory_mirror "
                "WHERE status = 'superseded' AND index_disabled_at = '' "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
