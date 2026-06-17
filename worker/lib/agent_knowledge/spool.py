from __future__ import annotations

import json
import os
from pathlib import Path

from .events import validate_event


class Spool:
    SUBDIRS = ("pending", "processing", "acked", "quarantine")

    def __init__(self, root: Path | str):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("spool root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in self.SUBDIRS:
            path = self.root / name
            if path.is_symlink():
                raise ValueError(f"spool subdirectory must not be a symlink: {name}")
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)

    def enqueue(self, event: dict) -> Path:
        validate_event(event)
        name = f"{event['event_id']}.json"
        existing = self._find_existing(name)
        if existing is not None:
            return existing
        final_path = self.root / "pending" / name
        temp_path = self.root / "pending" / f".{name}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp_path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(event, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, final_path)
            os.chmod(final_path, 0o600)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            finally:
                raise
        return final_path

    def _find_existing(self, name: str) -> Path | None:
        for subdir in self.SUBDIRS:
            candidate = self.root / subdir / name
            if candidate.exists():
                return candidate
        return None

    def claim_next(self) -> Path:
        pending = sorted((self.root / "pending").glob("*.json"))
        if not pending:
            raise FileNotFoundError("no pending spool events")
        source = pending[0]
        target = self.root / "processing" / source.name
        os.replace(source, target)
        return target

    def ack(self, processing_path: Path | str) -> Path:
        source = Path(processing_path)
        target = self.root / "acked" / source.name
        os.replace(source, target)
        return target

    def quarantine(self, processing_path: Path | str) -> Path:
        source = Path(processing_path)
        target = self.root / "quarantine" / source.name
        os.replace(source, target)
        return target

    def depth_counts(self) -> dict[str, int]:
        return {name: len(list((self.root / name).glob("*.json"))) for name in self.SUBDIRS}
