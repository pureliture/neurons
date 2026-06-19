from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge.ledger import Ledger

from .ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .models import SessionMemoryArtifact, SourceRefRecord

ARCHIVE_SCHEMA_VERSION = "llm_brain_portable_archive.v1"
DATA_DIR = "data"
SPEC_DIR = "specs/llm-brain-core-v1"
MEMORY_CARDS_FILE = f"{DATA_DIR}/llm_brain_memory_cards.jsonl"
ARTIFACTS_FILE = f"{DATA_DIR}/llm_brain_session_memory_artifacts.jsonl"
SOURCE_REFS_FILE = f"{DATA_DIR}/llm_brain_source_refs.jsonl"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True)
class PortableArchiveReport:
    status: str
    archive_path: str
    counts: dict[str, int]
    manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "archive_path": self.archive_path,
            "counts": dict(self.counts),
            "manifest": dict(self.manifest),
        }


def export_llm_brain_archive(
    *,
    ledger_path: str | Path,
    output_path: str | Path,
    repo_root: str | Path | None = None,
) -> PortableArchiveReport:
    ledger = Ledger.open_read_only(ledger_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(repo_root) if repo_root is not None else _repo_root()
    with tempfile.TemporaryDirectory(prefix="llm-brain-export-") as tmp:
        staging = Path(tmp) / "archive"
        data_dir = staging / DATA_DIR
        data_dir.mkdir(parents=True)
        cards = _export_table_json(
            ledger,
            table="llm_brain_memory_cards",
            json_column="envelope_json",
            order_by="updated_at, memory_id",
        )
        artifacts = _export_table_json(
            ledger,
            table="llm_brain_session_memory_artifacts",
            json_column="artifact_json",
            order_by="updated_at, artifact_id",
        )
        source_refs = _export_table_json(
            ledger,
            table="llm_brain_source_refs",
            json_column="record_json",
            order_by="updated_at, source_ref_id",
        )
        _write_jsonl(staging / MEMORY_CARDS_FILE, cards)
        _write_jsonl(staging / ARTIFACTS_FILE, artifacts)
        _write_jsonl(staging / SOURCE_REFS_FILE, source_refs)
        _copy_specs(root, staging)
        counts = {
            "memory_cards": len(cards),
            "session_memory_artifacts": len(artifacts),
            "source_refs": len(source_refs),
        }
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "neurons.llm_brain_core",
            "raw_tables_included": False,
            "graph_db_files_included": False,
            "counts": counts,
            "files": [
                MANIFEST_FILE,
                MEMORY_CARDS_FILE,
                ARTIFACTS_FILE,
                SOURCE_REFS_FILE,
            ],
        }
        (staging / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_archive(staging, output)
    return PortableArchiveReport(status="exported", archive_path=str(output), counts=counts, manifest=manifest)


def import_llm_brain_archive(
    *,
    ledger_path: str | Path,
    archive_path: str | Path,
) -> PortableArchiveReport:
    ledger = Ledger(ledger_path)
    archive = Path(archive_path)
    with tempfile.TemporaryDirectory(prefix="llm-brain-import-") as tmp:
        staging = Path(tmp) / "archive"
        staging.mkdir()
        _extract_archive(archive, staging)
        manifest = _read_manifest(staging)
        if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            raise ValueError("unsupported llm-brain archive schema")
        cards = _read_jsonl(staging / MEMORY_CARDS_FILE)
        artifacts = _read_jsonl(staging / ARTIFACTS_FILE)
        source_refs = _read_jsonl(staging / SOURCE_REFS_FILE)
        for card in cards:
            ledger.upsert_llm_brain_memory_card(card)
        artifact_store = LedgerSessionMemoryArtifactStore(ledger)
        for artifact in artifacts:
            artifact_store.upsert(_artifact_from_dict(artifact))
        source_catalog = LedgerSourceRefCatalog(ledger)
        for source_ref in source_refs:
            source_catalog.register(_source_ref_from_dict(source_ref))
    counts = {
        "memory_cards": len(cards),
        "session_memory_artifacts": len(artifacts),
        "source_refs": len(source_refs),
    }
    return PortableArchiveReport(status="imported", archive_path=str(archive), counts=counts, manifest=manifest)


def _export_table_json(ledger: Ledger, *, table: str, json_column: str, order_by: str) -> list[dict[str, Any]]:
    try:
        with ledger._connect() as connection:
            rows = connection.execute(
                f"SELECT {json_column} FROM {table} ORDER BY {order_by}"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if f"no such table: {table}" in str(exc):
            return []
        raise
    records: list[dict[str, Any]] = []
    for row in rows:
        parsed = json.loads(str(row[json_column]))
        if not isinstance(parsed, dict):
            raise ValueError(f"{table}.{json_column} must contain JSON objects")
        records.append(parsed)
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError(f"{path.name} must contain JSON objects")
            records.append(parsed)
    return records


def _copy_specs(repo_root: Path, staging: Path) -> None:
    source_dir = repo_root / SPEC_DIR
    if not source_dir.exists():
        return
    target_dir = staging / SPEC_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ("requirements.md", "design.md", "implementation-matrix.md"):
        source = source_dir / name
        if source.exists():
            shutil.copy2(source, target_dir / name)


def _write_archive(staging: Path, output: Path) -> None:
    if output.name.endswith(".tar.zst"):
        _write_tar_zst(staging, output)
        return
    mode = "w:gz" if output.name.endswith((".tar.gz", ".tgz")) else "w"
    with tarfile.open(output, mode) as tar:
        for path in sorted(staging.rglob("*")):
            tar.add(path, arcname=path.relative_to(staging))


def _write_tar_zst(staging: Path, output: Path) -> None:
    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError("zstd command is required for .tar.zst archives")
    with tempfile.NamedTemporaryFile(prefix="llm-brain-", suffix=".tar") as tmp_tar:
        with tarfile.open(tmp_tar.name, "w") as tar:
            for path in sorted(staging.rglob("*")):
                tar.add(path, arcname=path.relative_to(staging))
        subprocess.run([zstd, "-q", "-f", "-o", str(output), tmp_tar.name], check=True)


def _extract_archive(archive: Path, target: Path) -> None:
    if archive.name.endswith(".tar.zst"):
        zstd = shutil.which("zstd")
        if not zstd:
            raise RuntimeError("zstd command is required for .tar.zst archives")
        tmp_tar = tempfile.NamedTemporaryFile(prefix="llm-brain-import-", suffix=".tar", delete=False)
        tmp_tar_path = Path(tmp_tar.name)
        tmp_tar.close()
        try:
            with tmp_tar_path.open("wb") as handle:
                subprocess.run([zstd, "-q", "-d", "-c", str(archive)], check=True, stdout=handle)
            _extract_tar(tmp_tar_path, target, mode="r")
        finally:
            tmp_tar_path.unlink(missing_ok=True)
        return
    mode = "r:gz" if archive.name.endswith((".tar.gz", ".tgz")) else "r"
    _extract_tar(archive, target, mode=mode)


def _extract_tar(archive: Path, target: Path, *, mode: str) -> None:
    with tarfile.open(archive, mode) as tar:
        members = tar.getmembers()
        for member in members:
            _validate_member(member)
        tar.extractall(target, members=members, filter="data")


def _validate_member(member: tarfile.TarInfo) -> None:
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("archive member path escapes target")
    if member.issym() or member.islnk():
        raise ValueError("archive links are not allowed")


def _read_manifest(staging: Path) -> dict[str, Any]:
    manifest_path = staging / MANIFEST_FILE
    if not manifest_path.exists():
        raise ValueError("llm-brain archive manifest is missing")
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("llm-brain archive manifest must be an object")
    return parsed


def _artifact_from_dict(parsed: dict[str, Any]) -> SessionMemoryArtifact:
    return SessionMemoryArtifact(
        artifact_id=str(parsed["artifact_id"]),
        session_id_hash=str(parsed["session_id_hash"]),
        project=str(parsed["project"]),
        provider=str(parsed["provider"]),
        source_event_ids=tuple(parsed.get("source_event_ids") or ()),
        chunk_refs=tuple(parsed.get("chunk_refs") or ()),
        tool_evidence_refs=tuple(parsed.get("tool_evidence_refs") or ()),
        summary=str(parsed["summary"]),
        content_hash=str(parsed["content_hash"]),
        ontology_version=str(parsed.get("ontology_version") or "1.0.0"),
        extractor_version=str(parsed.get("extractor_version") or "0.1.0"),
        created_at=str(parsed.get("created_at") or ""),
    )


def _source_ref_from_dict(parsed: dict[str, Any]) -> SourceRefRecord:
    return SourceRefRecord(
        source_ref_id=str(parsed["source_ref_id"]),
        device_id_hash=str(parsed["device_id_hash"]),
        root_id=str(parsed["root_id"]),
        relative_path_hash=str(parsed["relative_path_hash"]),
        content_hash=str(parsed["content_hash"]),
        mtime=str(parsed["mtime"]),
        size=int(parsed["size"]),
        sync_policy=parsed["sync_policy"],
        permission_scope=str(parsed.get("permission_scope") or "project"),
        last_seen_at=str(parsed.get("last_seen_at") or ""),
        deleted_at=str(parsed.get("deleted_at") or ""),
        revoked_at=str(parsed.get("revoked_at") or ""),
        derived_summary=str(parsed.get("derived_summary") or ""),
        redacted_content=str(parsed.get("redacted_content") or ""),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]
