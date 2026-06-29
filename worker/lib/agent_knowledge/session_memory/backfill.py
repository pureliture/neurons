from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path

from ..ledger import Ledger
from .transcript_model import canonicalize_project
from .transcript_parsers import parse_transcript_source
from .transcript_quality import audit_transcript_source


SUPPORTED_PROVIDERS = {"claude", "gemini", "codex"}
FIXTURE_ROOT_SENTINEL = ".agent-knowledge-backfill-fixture-root"
APPROVAL_REQUIRED_FIELDS = [
    "exact_private_source_roots",
    "exact_argv",
    "timeout_seconds",
    "redaction_required",
    "abort_criteria",
    "rollback_owner",
    "index_write_approval",
]


def inventory_fixture_sources(
    *,
    ledger: Ledger,
    fixture_source_root: Path | str,
    project: str,
) -> dict:
    root = Path(fixture_source_root)
    _validate_fixture_source_root(root)
    project = canonicalize_project(project)
    discovered = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == FIXTURE_ROOT_SENTINEL:
            continue
        provider = _provider_from_fixture_path(path, root)
        source_path_hash = _sha256(str(path))
        ledger.upsert_backfill_source(
            {
                "source_id": source_path_hash,
                "raw_source_path": str(path),
                "source_path_hash": source_path_hash,
                "project": project,
                "provider": provider,
                "inventory_status": "discovered",
            }
        )
        discovered += 1
    return {
        "schema_version": "agent_knowledge_backfill_inventory.v1",
        "status": "inventory_recorded",
        "private_source_scan_performed": False,
        "network_used": False,
        "live_mutation_allowed": False,
        "summary": {
            "discovered": discovered,
            "raw_paths_redacted": True,
        },
    }


def dry_run_backfill(
    *,
    ledger: Ledger,
    batch_limit: int,
    rate_limit_per_minute: int,
) -> dict:
    _validate_limits(batch_limit=batch_limit, rate_limit_per_minute=rate_limit_per_minute)
    contracts = {row["provider"]: row for row in ledger.list_provider_source_contracts()}
    sources = []
    quarantine_items = []

    for row in ledger.list_backfill_sources():
        classified = _classify_source(row, contracts, ledger)
        redacted = _redacted_source(classified)
        sources.append(redacted)
        if classified["inventory_status"] == "quarantined":
            quarantine_items.append(
                {
                    "source_path_hash": classified["source_path_hash"],
                    "provider": classified["provider"],
                    "reason": classified["quarantine_reason"],
                }
            )

    return {
        "schema_version": "agent_knowledge_backfill_dry_run.v1",
        "status": "dry_run_ready",
        "private_source_scan_performed": False,
        "live_indexing_performed": False,
        "mutation_performed": False,
        "limits": {
            "batch_limit": batch_limit,
            "rate_limit_per_minute": rate_limit_per_minute,
        },
        "counts": _counts_for_sources(sources),
        "quarantine": {
            "count": len(quarantine_items),
            "items": quarantine_items,
        },
        "sources": sources,
    }


def build_execute_plan(
    *,
    ledger: Ledger,
    batch_limit: int,
    rate_limit_per_minute: int,
) -> dict:
    _validate_limits(batch_limit=batch_limit, rate_limit_per_minute=rate_limit_per_minute)
    ready_sources = [
        _redacted_source(row)
        for row in ledger.list_backfill_sources()
        if row["inventory_status"] == "ready"
    ]
    batches = [
        {
            "batch_index": index + 1,
            "max_items": batch_limit,
            "source_count": len(batch),
            "sources": batch,
        }
        for index, batch in enumerate(_chunks(ready_sources, batch_limit))
    ]
    return {
        "schema_version": "agent_knowledge_backfill_execute_plan.v1",
        "status": "plan_only",
        "requires_approval_before_execution": True,
        "private_source_scan_performed": False,
        "live_indexing_performed": False,
        "mutation_performed": False,
        "limits": {
            "batch_limit": batch_limit,
            "rate_limit_per_minute": rate_limit_per_minute,
        },
        "plan": {
            "ready_source_count": len(ready_sources),
            "batch_count": len(batches),
            "batches": batches,
        },
        "approval_required_fields": APPROVAL_REQUIRED_FIELDS,
    }


def _classify_source(row: dict, contracts: dict[str, dict], ledger: Ledger) -> dict:
    provider = row["provider"]
    contract = contracts.get(provider)
    provider_contract_status = contract["verification_status"] if contract else "unsupported_provider"
    source_contract_status = contract["source_status"] if contract else "unsupported_provider"
    quality_manifest = None

    if provider not in SUPPORTED_PROVIDERS or contract is None:
        parser_status = "unsupported_provider"
        inventory_status = "quarantined"
        quarantine_reason = "unsupported_provider"
    elif source_contract_status != "source_locator_verified":
        parser_status = "not_attempted_contract_unproven"
        inventory_status = "skipped"
        quarantine_reason = ""
    else:
        try:
            parse_transcript_source(
                provider,
                row["raw_source_path"],
                project=row["project"],
                source_locator_hash=row["source_path_hash"],
            )
        except ValueError as exc:
            parser_status = _parser_status_from_error(str(exc))
            inventory_status = "quarantined"
            quarantine_reason = parser_status
        except OSError:
            parser_status = "source_unreadable"
            inventory_status = "quarantined"
            quarantine_reason = parser_status
        else:
            try:
                quality_manifest = audit_transcript_source(
                    provider,
                    row["raw_source_path"],
                    project=row["project"],
                )
            except Exception:
                parser_status = "quality_audit_failed"
                inventory_status = "quarantined"
                quarantine_reason = parser_status
            else:
                parser_status = "parsed_ok"
                inventory_status = "ready"
                quarantine_reason = ""

    updated = ledger.update_backfill_source_status(
        row["source_path_hash"],
        provider_contract_status=provider_contract_status,
        source_contract_status=source_contract_status,
        parser_status=parser_status,
        inventory_status=inventory_status,
        quarantine_reason=quarantine_reason,
    )
    if quality_manifest is not None:
        updated["quality_manifest"] = quality_manifest
    return updated


def _counts_for_sources(sources: list[dict]) -> dict:
    by_status = Counter(source["inventory_status"] for source in sources)
    by_provider = Counter(source["provider"] for source in sources)
    by_provider_contract_status = Counter(source["provider_contract_status"] for source in sources)
    by_source_contract_status = Counter(source["source_contract_status"] for source in sources)
    by_parser_status = Counter(source["parser_status"] for source in sources)
    return {
        "by_status": {status: by_status.get(status, 0) for status in ["failed", "indexed", "quarantined", "ready", "skipped"]},
        "by_provider": dict(sorted(by_provider.items())),
        "by_provider_contract_status": dict(sorted(by_provider_contract_status.items())),
        "by_source_contract_status": dict(sorted(by_source_contract_status.items())),
        "by_parser_status": dict(sorted(by_parser_status.items())),
    }


def _redacted_source(row: dict) -> dict:
    redacted = {
        "source_path_hash": row["source_path_hash"],
        "project": row["project"],
        "provider": row["provider"],
        "provider_contract_status": row.get("provider_contract_status", ""),
        "source_contract_status": row.get("source_contract_status", ""),
        "parser_status": row.get("parser_status", ""),
        "inventory_status": row["inventory_status"],
        "quarantine_reason": row.get("quarantine_reason", ""),
    }
    if "quality_manifest" in row:
        redacted["quality_manifest"] = row["quality_manifest"]
    return redacted


def _provider_from_fixture_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).parts[0]
    except (IndexError, ValueError):
        return "unknown"


def _validate_fixture_source_root(root: Path) -> None:
    if root.name != "fixtures":
        raise ValueError("fixture-source-root must be an explicit fixture directory")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("fixture-source-root must be a non-symlink fixture directory")
    sentinel = root / FIXTURE_ROOT_SENTINEL
    if sentinel.is_symlink() or not sentinel.is_file():
        raise ValueError("fixture-source-root must contain the backfill fixture sentinel")


def _parser_status_from_error(message: str) -> str:
    if "unsupported provider" in message:
        return "unsupported_provider"
    if "source_unreadable" in message:
        return "source_unreadable"
    return "source_parse_failed"


def _validate_limits(*, batch_limit: int, rate_limit_per_minute: int) -> None:
    if batch_limit <= 0:
        raise ValueError("batch-limit must be positive")
    if rate_limit_per_minute <= 0:
        raise ValueError("rate-limit-per-minute must be positive")


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
