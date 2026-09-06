"""Fail-closed Qdrant-to-PostgreSQL migration sidecar (M5)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Callable

from ..model_connectors import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL
from .pgvector_store import MemoryCard, PgVectorStore, SessionChunk

logger = logging.getLogger(__name__)
_VERSION = 1
_DISTANCE = "cosine"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def _value(source: object, name: str, default: object = None) -> object:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _timestamp(value: object, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise ValueError("authority_valid_from_missing")
        return None
    if not isinstance(value, str):
        raise ValueError("authority_timestamp_invalid")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("authority_timestamp_invalid")
    return value


@dataclass
class MigrationResult:
    """Redacted report: no source payload, source ID, or exception text."""

    collection_name: str
    total_scanned: int = 0
    total_migrated: int = 0
    total_skipped: int = 0
    total_quarantined: int = 0
    outbox_enqueued: int = 0
    elapsed_seconds: float = 0.0
    dry_run: bool = False
    preflight: dict[str, object] = field(default_factory=dict)
    quarantined_records: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullMigrationSummary:
    started_at: str
    completed_at: str
    dry_run: bool
    session_chunks_result: MigrationResult
    memory_cards_result: MigrationResult
    total_migrated: int = 0
    total_quarantined: int = 0
    total_outbox_enqueued: int = 0
    total_elapsed_seconds: float = 0.0
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"started_at": self.started_at, "completed_at": self.completed_at, "dry_run": self.dry_run,
                "total_migrated": self.total_migrated, "total_quarantined": self.total_quarantined,
                "total_outbox_enqueued": self.total_outbox_enqueued, "total_elapsed_seconds": self.total_elapsed_seconds,
                "success": self.success, "session_chunks": self.session_chunks_result.to_dict(),
                "memory_cards": self.memory_cards_result.to_dict()}


class QdrantToPostgresMigrator:
    """Use only Qdrant ``get_collection``/``scroll`` and a real PG store seam."""

    def __init__(self, qdrant_client: Any, target_store: PgVectorStore, batch_size: int = 100,
                 dry_run: bool = False, checkpoint_file: str | None = None) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size_invalid")
        if not all(callable(getattr(qdrant_client, name, None)) for name in ("get_collection", "scroll")):
            raise ValueError("qdrant_api_seam_required")
        self.qdrant, self.target_store = qdrant_client, target_store
        self.batch_size, self.dry_run, self.checkpoint_file = batch_size, dry_run, checkpoint_file
        self.checkpoints = self._load_checkpoints()

    def _load_checkpoints(self) -> dict[str, Any]:
        if not self.checkpoint_file or not os.path.exists(self.checkpoint_file):
            return {"version": _VERSION, "collections": {}}
        try:
            with open(self.checkpoint_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("checkpoint_invalid") from exc
        if not isinstance(data, dict) or data.get("version") != _VERSION or not isinstance(data.get("collections"), dict):
            raise ValueError("checkpoint_invalid")
        for key, record in data["collections"].items():
            offset = record.get("next_offset") if isinstance(record, dict) else object()
            if not isinstance(key, str) or not isinstance(record, dict) or record.get("collection_digest") != key or not isinstance(record.get("completed"), bool) or not isinstance(record.get("preflight"), dict) or (offset is not None and not isinstance(offset, (str, int))):
                raise ValueError("checkpoint_invalid")
        return data

    def _save(self) -> None:
        if self.dry_run or not self.checkpoint_file:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.checkpoint_file)), exist_ok=True)
            with open(self.checkpoint_file, "w", encoding="utf-8") as handle:
                json.dump(self.checkpoints, handle, sort_keys=True)
        except OSError as exc:
            raise RuntimeError("checkpoint_write_failed") from exc

    def _preflight(self, name: str) -> dict[str, object]:
        collection = self.qdrant.get_collection(collection_name=name)
        vectors = _value(_value(_value(collection, "config"), "params"), "vectors")
        if isinstance(vectors, dict) and "size" not in vectors:
            vectors = next(iter(vectors.values())) if len(vectors) == 1 else None
        size, distance = _value(vectors, "size"), _value(vectors, "distance")
        distance = str(_value(distance, "value", distance)).lower()
        return {"collection_digest": _digest(name), "dimension": size if isinstance(size, int) else None,
                "distance": distance or None, "expected_dimension": DEFAULT_EMBEDDING_DIM,
                "expected_distance": _DISTANCE, "expected_model": DEFAULT_EMBEDDING_MODEL,
                "compatible": size == DEFAULT_EMBEDDING_DIM and distance == _DISTANCE}

    def _fetch_qdrant_points(self, name: str, offset: Any = None) -> tuple[list[Any], Any]:
        points, next_offset = self.qdrant.scroll(collection_name=name, limit=self.batch_size, offset=offset,
                                                 with_payload=True, with_vectors=True)
        return list(points), next_offset

    @staticmethod
    def _require(payload: dict[str, Any], key: str) -> Any:
        value = payload.get(key)
        if value is None or value == "":
            raise ValueError(f"required_{key}_missing")
        return value

    def _unpack(self, item: object) -> tuple[object, list[float] | None, dict[str, Any]]:
        point_id, vector, payload = _value(item, "id"), _value(item, "vector"), _value(item, "payload")
        if point_id is None or not isinstance(payload, dict):
            raise ValueError("point_shape_invalid")
        if vector is not None and (not isinstance(vector, list) or any(not isinstance(v, (int, float)) for v in vector)):
            raise ValueError("vector_shape_invalid")
        return point_id, vector, payload

    def _copyable(self, vector: list[float] | None, payload: dict[str, Any]) -> bool:
        if vector is None:
            return False
        if len(vector) != DEFAULT_EMBEDDING_DIM:
            raise ValueError("vector_dimension_mismatch")
        model = payload.get("embedding_model")
        if model is not None and model != DEFAULT_EMBEDDING_MODEL:
            raise ValueError("embedding_profile_mismatch")
        return True

    def _checkpoint(self, name: str, preflight: dict[str, object]) -> dict[str, Any]:
        key = _digest(name)
        checkpoint = self.checkpoints["collections"].get(key)
        if checkpoint is None:
            return {"collection_digest": key, "next_offset": None, "completed": False, "preflight": preflight}
        if checkpoint["preflight"] != preflight:
            raise ValueError("checkpoint_preflight_mismatch")
        return checkpoint

    @staticmethod
    def _reason_code(exc: Exception) -> str:
        # Quarantine reports carry closed-vocabulary reason codes only.
        # Raw exception text can echo payload-derived values, so pass through
        # just explicitly raised snake_case codes and mask everything else.
        message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
        if re.fullmatch(r"[a-z][a-z0-9_]*", message or ""):
            return message
        return f"{type(exc).__name__.lower()}_record_rejected"

    @staticmethod
    def _quarantine(result: MigrationResult, point_id: object | None, reason: str) -> None:
        result.total_quarantined += 1
        result.quarantined_records.append({"point_digest": _digest(point_id) if point_id is not None else "unknown", "reason_code": reason})

    def _migrate(self, name: str, project: str | None, build: Callable[..., Any], write: Callable[..., Any]) -> MigrationResult:
        started, preflight = time.time(), self._preflight(name)
        result = MigrationResult(collection_name=_digest(name), dry_run=self.dry_run, preflight=preflight)
        checkpoint = self._checkpoint(name, preflight)
        if checkpoint["completed"]:
            result.elapsed_seconds = round(time.time() - started, 4)
            return result
        if not preflight["compatible"]:
            result.errors.append("collection_profile_mismatch")
            result.elapsed_seconds = round(time.time() - started, 4)
            return result
        offset = checkpoint["next_offset"]
        while True:
            points, next_offset = self._fetch_qdrant_points(name, offset)
            if not points:
                break
            if not self.dry_run and hasattr(self.target_store, "_scope"):
                with self.target_store._scope(write=True) as batch_conn:
                    for item in points:
                        point_id = None
                        result.total_scanned += 1
                        try:
                            point_id, vector, payload = self._unpack(item)
                            if project is not None and payload.get("project") != project:
                                result.total_skipped += 1
                                continue
                            record = build(point_id, vector, payload)
                            write(record, conn=batch_conn)
                            if record.embedding is None:
                                result.outbox_enqueued += 1
                            result.total_migrated += 1
                        except (TypeError, ValueError, OverflowError) as exc:
                            self._quarantine(result, point_id, self._reason_code(exc))
                        except Exception:
                            logger.exception("migration_record_failed reason_code=target_write_failed")
                            self._quarantine(result, point_id, "target_write_failed")
            else:
                for item in points:
                    point_id = None
                    result.total_scanned += 1
                    try:
                        point_id, vector, payload = self._unpack(item)
                        if project is not None and payload.get("project") != project:
                            result.total_skipped += 1
                            continue
                        record = build(point_id, vector, payload)
                        if not self.dry_run:
                            write(record)
                            if record.embedding is None:
                                result.outbox_enqueued += 1
                        result.total_migrated += 1
                    except (TypeError, ValueError, OverflowError) as exc:
                        self._quarantine(result, point_id, self._reason_code(exc))
                    except Exception:
                        logger.exception("migration_record_failed reason_code=target_write_failed")
                        self._quarantine(result, point_id, "target_write_failed")
            offset = next_offset
            if not self.dry_run:
                checkpoint.update(next_offset=offset, completed=offset is None)
                self.checkpoints["collections"][_digest(name)] = checkpoint
                self._save()
            if offset is None:
                break
        result.elapsed_seconds = round(time.time() - started, 4)
        return result

    @staticmethod
    def _content_hash(payload: dict[str, Any]) -> str:
        value = QdrantToPostgresMigrator._require(payload, "content_hash")
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError("content_hash_invalid")
        return value

    def migrate_session_chunks(self, collection_name: str = "session_chunks", project: str | None = None) -> MigrationResult:
        def build(_id: object, vector: list[float] | None, payload: dict[str, Any]) -> SessionChunk:
            copied = self._copyable(vector, payload)
            chunk_id = str(payload.get("chunk_id") or payload.get("memory_id") or _id)[:128]
            session_id_hash = str(self._require(payload, "session_id_hash"))
            project_val = str(self._require(payload, "project"))[:128]
            provider_val = str(payload.get("provider") or "unspecified")[:64]
            raw_index = payload.get("chunk_index")
            if raw_index is None:
                raw_index = payload.get("turn_start_index")
            chunk_index = int(raw_index) if raw_index is not None else 0
            content_markdown = str(payload.get("content_markdown") or payload.get("text") or payload.get("summary") or "").replace("\x00", "")
            token_count = int(payload.get("token_count") or 0)
            return SessionChunk(
                chunk_id=chunk_id,
                session_id_hash=session_id_hash,
                project=project_val,
                provider=provider_val,
                chunk_index=chunk_index,
                content_markdown=content_markdown,
                token_count=token_count,
                content_hash=self._content_hash(payload),
                embedding_model=DEFAULT_EMBEDDING_MODEL,
                embedding_state="ready" if copied else "pending",
                embedding=list(vector) if (copied and vector is not None) else None,
            )
        return self._migrate(collection_name, project, build, self.target_store.insert_chunk)

    def migrate_memory_cards(self, collection_name: str = "memory_cards", project: str | None = None) -> MigrationResult:
        def build(_id: object, vector: list[float] | None, payload: dict[str, Any]) -> MemoryCard:
            copied, valid_from, valid_to = self._copyable(vector, payload), _timestamp(payload.get("valid_from"), True), _timestamp(payload.get("valid_to"), False)
            typed_payload, source_ref = self._require(payload, "typed_payload"), self._require(payload, "source_ref")
            if valid_to is not None and valid_to < valid_from:
                raise ValueError("authority_validity_invalid")
            if not isinstance(typed_payload, dict) or not isinstance(source_ref, list):
                raise ValueError("authority_shape_invalid")
            return MemoryCard(memory_id=str(self._require(payload, "memory_id")), project=str(self._require(payload, "project")), card_type=str(self._require(payload, "card_type")),
                title=str(self._require(payload, "title")), summary=str(self._require(payload, "summary")), typed_payload=typed_payload,
                lifecycle_state=str(self._require(payload, "lifecycle_state")), authorization_status=str(self._require(payload, "authorization_status")),
                currentness=str(self._require(payload, "currentness")), confidence=float(self._require(payload, "confidence")), valid_from=valid_from, valid_to=valid_to,
                embedding_model=DEFAULT_EMBEDDING_MODEL, embedding_state="ready" if copied else "pending", embedding=list(vector) if copied else None,
                content_hash=self._content_hash(payload), source_ref=source_ref)
        # Store-owned card+outbox transaction; never enqueue again in this sidecar.
        return self._migrate(collection_name, project, build, self.target_store.upsert_card)

    def run_full_migration(self, project: str | None = None) -> FullMigrationSummary:
        started_at, started = datetime.now(timezone.utc).isoformat(), time.time()
        chunks, cards = self.migrate_session_chunks(project=project), self.migrate_memory_cards(project=project)
        return FullMigrationSummary(started_at, datetime.now(timezone.utc).isoformat(), self.dry_run, chunks, cards,
            chunks.total_migrated + cards.total_migrated, chunks.total_quarantined + cards.total_quarantined,
            chunks.outbox_enqueued + cards.outbox_enqueued, round(time.time() - started, 4), not chunks.errors and not cards.errors)
