"""M7 write-only ingress journal: byte-faithful wire-payload persistence for replay.

At enqueue time the exact POSTed ``rag_ingress_enqueue.v1`` request body is recorded
here so a later replay can re-deliver the BYTE-IDENTICAL document, closing the M6
replay body-fidelity limitation (M6 replay reconstructs a best-effort body because the
original wire payload was never persisted).

Design constraints (from the M7 multi-agent review):
- **Write-only, no flush.** This module exposes ``record()`` + a ``knowledge_id``-keyed
  ``get()``/``count()`` only. It deliberately has NO ``flush()``/deliver/POST entry
  point and holds no HTTP client, so the journal itself can never perform a live
  enqueue. A replay re-flush is a separate, operator-gated path in ``replay_delivery``.
- **Indexed by ``knowledge_id``.** A replay row only knows its ``knowledge_id`` (not the
  original ``idempotencyKey``), so entries are keyed by the ``knowledge_id`` carried in
  ``payload.payload.document.metadata.knowledge_id``.
- **Private + redaction-safe.** 0700 dir / 0600 files, refuses a symlink parent. It
  stores the already-redaction.v2 wire body; it never returns or logs raw paths/ids.
- **Byte-faithful.** It persists the exact request-body dict it is handed; it never
  reconstructs or re-derives the payload.
- **Latest wins.** One entry per knowledge_id: re-recording the same knowledge_id
  overwrites the previous entry, so a replay re-flushes the MOST RECENT acked wire
  bytes for that knowledge_id, not an arbitrary historical version.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class IngressJournal:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("journal root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _knowledge_id_of(payload: dict) -> str:
        document = ((payload.get("payload") or {}).get("document") or {})
        metadata = document.get("metadata") or {}
        return str(metadata.get("knowledge_id") or "")

    def _entry_path(self, knowledge_id: str) -> Path:
        digest = hashlib.sha256(knowledge_id.encode("utf-8")).hexdigest()[:24]
        return self.root / f"entry_{digest}.json"

    def record(self, payload: dict) -> bool:
        """Persist the byte-faithful wire payload, keyed by its knowledge_id.

        Returns False (records nothing) if the payload carries no knowledge_id, since
        such an entry could never be looked up by a replay row.
        """
        knowledge_id = self._knowledge_id_of(payload)
        if not knowledge_id:
            return False
        final_path = self._entry_path(knowledge_id)
        temp_path = self.root / f".{final_path.name}.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, final_path)
            os.chmod(final_path, 0o600)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return True

    def get(self, knowledge_id: str) -> dict | None:
        """Return the byte-faithful wire payload recorded for knowledge_id, or None."""
        if not knowledge_id:
            return None
        path = self._entry_path(knowledge_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def count(self) -> int:
        return len(list(self.root.glob("entry_*.json")))
