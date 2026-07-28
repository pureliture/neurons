from __future__ import annotations

from ..ledger import Ledger
from .native_memory_write_runner import run_native_memory_sync


class NativeMemoryEngine:
    """Native Memory sync 호출자를 위한 additive façade.

    기존 Store/Writer/Reconcile 경계와 report shape는
    ``run_native_memory_sync``가 계속 소유한다. 이 façade는 호출자가
    orchestration 세부사항에 직접 의존하지 않도록 안정적인 진입점만 제공한다.
    """

    def __init__(
        self,
        *,
        ledger: Ledger,
        retired_index_bridge: object | None,
        memory_id: str,
        agent_id: str = "native-memory-sync",
        user_id: str = "",
        batch_limit: int = 200,
        reconcile_top_n: int = 50,
    ):
        self._ledger = ledger
        self._retired_index_bridge = retired_index_bridge
        self._memory_id = memory_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._batch_limit = batch_limit
        self._reconcile_top_n = reconcile_top_n

    def sync_session_memory(self, *, dry_run: bool) -> dict:
        """기존 write → reconcile orchestration을 실행한다.

        ``dry_run=False``는 주입된 bridge와 ledger에 mutation을 수행할 수 있다.
        호출자가 실행 의도를 반드시 명시하도록 기본값을 두지 않는다.
        """
        return run_native_memory_sync(
            ledger=self._ledger,
            retired_index_bridge=self._retired_index_bridge,
            memory_id=self._memory_id,
            agent_id=self._agent_id,
            user_id=self._user_id,
            batch_limit=self._batch_limit,
            reconcile_top_n=self._reconcile_top_n,
            dry_run=dry_run,
        )
