"""Ledger Core DB engine adapter seam (Phase B → C 전제).

``Ledger``와 raw-SQL 모듈이 ``sqlite3``에 직접 묶이는 대신 ``ILedgerCoreDbAdapter``를 통해
connection을 얻게 한다. 연결 레이어를 한 점으로 모아, Phase C에서 PostgreSQL 어댑터를
주입해 엔진을 교체할 수 있게 한다.

주의(Phase B 범위): 이 seam은 *연결 생성*을 추상화한다. SQL dialect 포팅(SQLite 전용
``INSERT OR IGNORE`` / ``ON CONFLICT`` / ``PRAGMA`` 등을 엔진 중립으로)은 C-spec의 별도
과제다. B는 "연결이 한 어댑터 경계를 지난다"를 확립한다(필요조건, 충분조건 아님).
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

SQLITE_BUSY_TIMEOUT_MS = 60000


class ClosingSqliteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class ILedgerCoreDbAdapter(ABC):
    """Ledger의 DB 엔진 접근을 격리하는 seam."""

    @abstractmethod
    def connect(self, *, configure_journal: bool = False):
        """새 DB connection을 연다. ``with`` 종료 시 close되는 객체를 돌려준다."""


class SqliteLedgerDbAdapter(ILedgerCoreDbAdapter):
    """현행 SQLite 엔진 어댑터.

    기존 ``Ledger._connect`` 로직을 그대로 옮긴 것이라 동작은 byte-identical하다
    (read-only URI 모드, busy_timeout/WAL/synchronous PRAGMA, sidecar 권한 하드닝 포함).
    """

    def __init__(self, path: Path | str, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = bool(read_only)

    def connect(self, *, configure_journal: bool = False) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                factory=ClosingSqliteConnection,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
            connection.execute("PRAGMA query_only=ON;")
            return connection
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            factory=ClosingSqliteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
        if configure_journal:
            connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA synchronous=NORMAL;")
        # WAL/SHM sidecar 권한을 connection 후 동적으로 하드닝.
        for p in self.path.parent.glob(f"{self.path.name}*"):
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        return connection
