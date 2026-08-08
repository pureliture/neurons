"""Live migration driver: Mac provider transcripts -> CouchDB source store.

Runs on the Mac (where the provider transcripts live; the Ubuntu server does not
read provider paths). For each provider it enumerates the on-disk session store,
extracts the session's own working directory (the authoritative ``project``
signal -- NOT RetiredIndexBridge's polluted metadata), and imports each session through the
``couchdb_source`` historical-import pipeline into a CouchDB store.

Provider roots (overridable):
  codex        ~/.codex/sessions/**/*.jsonl              (cwd from payload.cwd)
  claude       ~/.claude/projects/**/*.jsonl             (cwd from record.cwd)
  gemini       ~/.gemini/tmp/*/chats/*.{jsonl,json}      (.json -> fixture; project from <proj> dir)
  antigravity  ~/.gemini/antigravity/**/.system_generated/**/*.jsonl  (agy is captured here too)
  grok         $(GROK_HOME|~/.grok)/sessions/**/updates.jsonl  (project via capture metadata; ACP has no top-level cwd)

The store target comes from env (COUCHDB_URL / COUCHDB_USER / COUCHDB_PASSWORD /
COUCHDB_DB); ``--dry-run`` uses an in-memory store so coverage and project
resolution can be inspected without any write.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from .couchdb_http_store import CouchDBHttpSourceStore
from .current_source_supersession import (
    CURRENT_SOURCE_IMPORTED,
    activate_admitted_codex_current_source,
)
from .historical_import import ImportStatus, SourceLocator, import_historical_source
from .session_memory_materializer import update_coverage_with_tool_evidence
from .source_store import InMemoryCouchDBSourceStore
from .tool_evidence_bundler import store_tool_evidence_bundles
from .document_model import build_source_locator_hash, session_doc_id
from ..session_memory.native_memory_sync_approval import ApprovalError, validate_memory_enqueue_approval
from ..session_memory.transcript_model import canonicalize_project
from ..session_memory.transcript_parsers.common import LocatorAdmission
from ..session_memory.transcript_parsers.providers.codex import admit_codex_locator_snapshot
from ..session_memory.transcript_parsers import (
    extract_tool_evidence,
    parse_transcript_source,
)

MIGRATION_CLI_SCHEMA_VERSION = "transcript_migration_cli.v1"
MIGRATION_CLI_OPERATION = "transcript_migration"

MIGRATION_PROVIDERS = ("codex", "claude", "gemini", "antigravity", "grok")
_CWD_SCAN_MAX_LINES = 50
_CORRECTIVE_LOCATOR_ADMISSION_MANIFEST_FIELDS = frozenset({"locator", "admission", "project"})
_LOCATOR_ADMISSION_FIELDS = frozenset(
    {
        "expected_raw_sha256",
        "expected_byte_count",
        "max_bytes",
        "max_line_bytes",
        "max_record_count",
        "max_pending_tool_calls",
    }
)


def default_source_roots() -> dict[str, Path]:
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
    grok_home = Path(os.environ.get("GROK_HOME") or (home / ".grok"))
    return {
        "codex": codex_home / "sessions",
        "claude": home / ".claude" / "projects",
        "gemini": home / ".gemini" / "tmp",
        "antigravity": home / ".gemini" / "antigravity",
        "grok": grok_home / "sessions",
    }


def enumerate_provider_files(provider: str, root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    if provider == "gemini":
        files = [p for p in root.glob("*/chats/*.jsonl") if p.is_file()]
        files += [p for p in root.glob("*/chats/*.json") if p.is_file()]
        return sorted(files)
    if provider == "antigravity":
        return sorted(p for p in root.glob("**/.system_generated/**/*.jsonl") if p.is_file())
    if provider == "grok":
        # Session SoT is updates.jsonl per session directory (see Grok 17-sessions.md).
        return sorted(p for p in root.glob("**/updates.jsonl") if p.is_file() and not p.is_symlink())
    return sorted(p for p in root.glob("**/*.jsonl") if p.is_file() and not p.is_symlink())


def _source_file_fingerprint(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return the bounded identity needed to reject a changed scan member."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _tool_evidence_source_session_id(
    *,
    provider: str,
    path: Path,
    runtime_dir: Path,
) -> str:
    """Read only a source identity when a post-scan sibling appears."""

    source_path = path
    if provider == "gemini" and path.suffix == ".json":
        source_path = convert_gemini_json_to_fixture(path, runtime_dir)
    parsed = parse_transcript_source(
        provider,
        str(source_path),
        project="",
        source_locator_hash=build_source_locator_hash(str(source_path)),
    )
    session_id_hash = str(parsed.session.session_id_hash or "")
    if not session_id_hash:
        raise ValueError("tool evidence source session contract is invalid")
    return session_id_hash


def _iter_jsonl(path: Path, max_lines: int):
    try:
        with path.open(encoding="utf-8") as handle:
            for i, line in enumerate(handle):
                if i >= max_lines:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def extract_cwd(provider: str, path: Path) -> str:
    """Best-effort: the session's own working directory (authoritative project)."""
    if provider == "gemini" and path.suffix == ".json":
        # gemini .json: project derived from the <proj>/chats/ path segment instead
        return ""
    if provider == "grok":
        # Grok updates.jsonl is ACP session/update stream without top-level cwd;
        # project authority comes from sessions/<encoded-cwd>/ via
        # ``_grok_project_from_path`` (capture metadata) or dendrite metadata.
        return ""
    for record in _iter_jsonl(path, _CWD_SCAN_MAX_LINES):
        if not isinstance(record, dict):
            continue
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        payload = record.get("payload")
        if isinstance(payload, dict):
            pcwd = payload.get("cwd")
            if isinstance(pcwd, str) and pcwd:
                return pcwd
        for key in ("workspacePath", "workspace_path", "currentWorkingDirectory"):
            v = record.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _grok_project_from_path(path: Path) -> str:
    """Derive project from Grok layout ``sessions/<encoded-cwd>/<session-id>/updates.jsonl``.

    Never returns the SoT basename ``updates.jsonl``. When the group name is a
    URL-encoded absolute path, decode and canonicalize it. Opaque slug groups
    yield empty string so project authority stays unresolved/ambiguous rather
    than inventing a fake label.
    """
    path = Path(path)
    if path.name != "updates.jsonl":
        return ""
    parts = [part for part in path.parts if part]
    # Prefer trailing layout sessions/<encoded-cwd>/<session-id>/updates.jsonl
    # so an earlier path segment named "sessions" cannot steal the index.
    if len(parts) < 4 or parts[-4].lower() != "sessions":
        return ""
    encoded_cwd = parts[-3]
    decoded = unquote(encoded_cwd)
    if not decoded or decoded == encoded_cwd:
        return ""
    if "/" not in decoded and "\\" not in decoded:
        return ""
    return canonicalize_project(decoded)


def _gemini_project_from_path(path: Path) -> str:
    # ~/.gemini/tmp/<proj-hash-or-name>/chats/<file>
    parts = path.parts
    if "chats" in parts:
        idx = parts.index("chats")
        if idx > 0:
            return parts[idx - 1]
    return ""


def convert_gemini_json_to_fixture(path: Path, runtime_dir: Path) -> Path:
    """Convert a gemini ``.json`` chat ({sessionId, messages:[...]}) into a
    ``provider_transcript_fixture.v1`` file the parser accepts."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("source_parse_failed: gemini json root must be an object")
    session_id = str(data.get("sessionId") or data.get("session_id") or path.stem)
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("source_parse_failed: gemini json missing messages")
    turns = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = "assistant" if str(msg.get("type") or msg.get("role") or "").lower() in {"gemini", "model", "assistant"} else "user"
        content = msg.get("content")
        if isinstance(content, list):
            text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        else:
            text = str(content or "")
        if not text:
            continue
        turns.append({"role": role, "text": text, "timestamp": str(msg.get("timestamp") or "")})
    if not turns:
        raise ValueError("source_parse_failed: gemini json produced no turns")
    fixture = {
        "provider": "gemini",
        "schema_version": "provider_transcript_fixture.v1",
        "session_id": session_id,
        "started_at": turns[0].get("timestamp", ""),
        "ended_at": turns[-1].get("timestamp", ""),
        "turns": turns,
    }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out = runtime_dir / f"gemini-{session_id}-{abs(hash(str(path))) % (10**8):08d}.json"
    out.write_text(json.dumps(fixture), encoding="utf-8")
    return out


def build_store_from_env():
    url = os.environ.get("COUCHDB_URL")
    db = os.environ.get("COUCHDB_DB", "transcript_source")
    user = os.environ.get("COUCHDB_USER", "")
    password = os.environ.get("COUCHDB_PASSWORD", "")
    if not url:
        raise ValueError("COUCHDB_URL is required for a live migration (or use --dry-run)")
    auth_header = ""
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {token}"
    store = CouchDBHttpSourceStore(base_url=url, db=db, auth_header=auth_header)
    store.ensure_database()
    return store


def run_migration(
    *,
    store,
    roots: dict[str, Path] | None = None,
    providers: list[str] | None = None,
    limit: int | None = None,
    runtime_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    roots = roots if roots is not None else default_source_roots()
    providers = providers or list(MIGRATION_PROVIDERS)
    runtime_dir = runtime_dir or (Path.home() / ".config" / "neurons" / "gemini-normalized")
    report: dict = {"dry_run": dry_run, "by_provider": {}, "imported": 0, "ambiguous": 0, "mismatch": 0, "errors": 0}

    for provider in providers:
        root = roots.get(provider)
        files = enumerate_provider_files(provider, Path(root)) if root else []
        if limit is not None:
            files = files[: max(limit, 0)]
        prov = {"root": str(root or ""), "found": len(files), "imported": 0, "ambiguous": 0, "mismatch": 0, "errors": 0}
        for path in files:
            try:
                source_path = path
                # gemini transcripts carry no cwd; the readable project is the
                # ~/.gemini/tmp/<project>/chats/ path segment (for both .jsonl and
                # .json). Derive it from the ORIGINAL path before any conversion.
                gemini_project = _gemini_project_from_path(path) if provider == "gemini" else ""
                if provider == "gemini" and path.suffix == ".json":
                    source_path = convert_gemini_json_to_fixture(path, runtime_dir)
                cwd = extract_cwd(provider, path)
                # The session's own cwd is the authoritative project signal (the
                # capture-metadata tier). Codex session paths are date-based (no
                # project), so passing the file path as the only signal would
                # falsely conflict with cwd; cwd must win as capture metadata.
                capture_project = cwd
                if not capture_project:
                    if provider == "antigravity":
                        capture_project = "antigravity"
                    elif provider == "gemini":
                        capture_project = gemini_project
                    elif provider == "grok":
                        # Prefer URL-decoded sessions/<encoded-cwd>/ group; never
                        # fall back to the updates.jsonl basename as project.
                        capture_project = _grok_project_from_path(path)
                result = import_historical_source(
                    locator=SourceLocator(
                        provider=provider,
                        source_path=str(source_path),
                        capture_metadata_project=capture_project,
                        cwd=cwd,
                    ),
                    store=store,
                )
                if result.status == ImportStatus.IMPORTED:
                    prov["imported"] += 1
                    if result.project_ambiguous:
                        prov["ambiguous"] += 1
                    if result.index_project_mismatch:
                        prov["mismatch"] += 1
                else:
                    prov["errors"] += 1
            except Exception:  # noqa: BLE001 - per-file fail-soft
                prov["errors"] += 1
        report["by_provider"][provider] = prov
        for k in ("imported", "ambiguous", "mismatch", "errors"):
            report[k] += prov[k]
    return report


def run_tool_evidence(
    *,
    store,
    roots: dict[str, Path] | None = None,
    providers: list[str] | None = None,
    limit: int | None = None,
    runtime_dir: Path | None = None,
) -> dict:
    """Second pass: extract tool_evidence_summary per session file and store it as
    bounded tool_evidence_bundle docs in CouchDB. Idempotent (deterministic ids).

    ``limit`` bounds complete session replacements, not individual source files:
    a full generation cannot safely truncate an already-discovered sibling.
    The report separates the discovered source count from selected sessions.
    """
    roots = roots if roots is not None else default_source_roots()
    providers = providers or list(MIGRATION_PROVIDERS)
    runtime_dir = runtime_dir or (Path.home() / ".config" / "neurons" / "gemini-normalized")
    report: dict = {"by_provider": {}, "bundles": 0, "sessions_with_evidence": 0, "errors": 0}
    for provider in providers:
        root = roots.get(provider)
        files = enumerate_provider_files(provider, Path(root)) if root else []
        prov = {
            "found": len(files),
            "scanned_sources": len(files),
            "selected_sessions": 0,
            "bundles": 0,
            "sessions": 0,
            "errors": 0,
        }
        source_fingerprints = {
            path: _source_file_fingerprint(path) for path in files
        }
        records_by_session: dict[str, list] = {}
        source_paths_by_session: dict[str, set[Path]] = {}
        session_id_by_source_path: dict[Path, str] = {}
        incomplete_session_ids: set[str] = set()
        has_unattributed_source_failure = False
        for path in files:
            source_path = path
            source_locator_hash = ""
            try:
                if provider == "gemini" and path.suffix == ".json":
                    source_path = convert_gemini_json_to_fixture(path, runtime_dir)
                source_locator_hash = build_source_locator_hash(str(source_path))
                records = extract_tool_evidence(
                    provider,
                    str(source_path),
                    project="",
                    source_locator_hash=source_locator_hash,
                )
                if not records:
                    parsed = parse_transcript_source(
                        provider,
                        str(source_path),
                        project="",
                        source_locator_hash=source_locator_hash,
                    )
                    session_id_hash = str(parsed.session.session_id_hash or "")
                else:
                    session_id_hashes = {
                        str(record.session_id_hash or "") for record in records
                    }
                    if len(session_id_hashes) != 1:
                        raise ValueError("tool evidence source session contract is invalid")
                    session_id_hash = next(iter(session_id_hashes))
                if not session_id_hash:
                    raise ValueError("tool evidence source session contract is invalid")
                records_by_session.setdefault(session_id_hash, []).extend(records)
                source_paths_by_session.setdefault(session_id_hash, set()).add(path)
                session_id_by_source_path[path] = session_id_hash
            except Exception:  # noqa: BLE001 - per-file fail-soft
                prov["errors"] += 1
                try:
                    if not source_locator_hash:
                        raise ValueError("tool evidence failed before source admission")
                    parsed = parse_transcript_source(
                        provider,
                        str(source_path),
                        project="",
                        source_locator_hash=source_locator_hash,
                    )
                    session_id_hash = str(parsed.session.session_id_hash or "")
                    if not session_id_hash:
                        raise ValueError("tool evidence source session contract is invalid")
                    incomplete_session_ids.add(session_id_hash)
                    source_paths_by_session.setdefault(session_id_hash, set()).add(path)
                    session_id_by_source_path[path] = session_id_hash
                except Exception:  # noqa: BLE001 - source identity is unknown
                    has_unattributed_source_failure = True

        # A full generation may only replace evidence from a stable provider
        # snapshot. If an existing source changes, disappears, or a same-session
        # sibling arrives during extraction, defer that session to the next run.
        unstable_session_ids: set[str] = set()
        unattributed_stability_failure = False
        for path, fingerprint in source_fingerprints.items():
            if fingerprint is None or _source_file_fingerprint(path) != fingerprint:
                session_id_hash = session_id_by_source_path.get(path, "")
                if session_id_hash:
                    unstable_session_ids.add(session_id_hash)
                else:
                    unattributed_stability_failure = True

        current_files = enumerate_provider_files(provider, Path(root)) if root else []
        initial_paths = set(files)
        current_paths = set(current_files)
        for path in initial_paths - current_paths:
            session_id_hash = session_id_by_source_path.get(path, "")
            if session_id_hash:
                unstable_session_ids.add(session_id_hash)
            else:
                unattributed_stability_failure = True
        for path in current_paths - initial_paths:
            try:
                unstable_session_ids.add(
                    _tool_evidence_source_session_id(
                        provider=provider,
                        path=path,
                        runtime_dir=runtime_dir,
                    )
                )
            except Exception:  # noqa: BLE001 - source identity is unknown
                unattributed_stability_failure = True

        if unstable_session_ids:
            incomplete_session_ids.update(unstable_session_ids)
            prov["errors"] += len(unstable_session_ids)
        if unattributed_stability_failure:
            has_unattributed_source_failure = True
            prov["errors"] += 1

        # Select only complete, stable generations. Applying ``limit`` before
        # this fence can spend the whole bounded run on a session that must be
        # skipped, despite another stable session being available.
        session_id_hashes = (
            []
            if has_unattributed_source_failure
            else sorted(
                session_id_hash
                for session_id_hash in records_by_session
                if session_id_hash not in incomplete_session_ids
            )
        )
        if limit is not None:
            # Full-generation replacement has to see every sibling source for
            # the selected session. Limit writes by complete sessions, rather
            # than truncating the source-file list before grouping.
            session_id_hashes = session_id_hashes[: max(limit, 0)]
        prov["selected_sessions"] = len(session_id_hashes)
        for session_id_hash in session_id_hashes:
            if (
                has_unattributed_source_failure
                or session_id_hash in incomplete_session_ids
            ):
                continue
            if any(
                _source_file_fingerprint(path) != source_fingerprints.get(path)
                for path in source_paths_by_session.get(session_id_hash, set())
            ):
                # Close the post-enumeration window without a global lock. The
                # caller can retry this bounded provider scan after sources settle.
                incomplete_session_ids.add(session_id_hash)
                prov["errors"] += 1
                continue
            session_records = records_by_session[session_id_hash]
            try:
                # A provider extractor reads the whole source it was given.
                # Group every matching source first, then publish exactly one
                # full-session replacement so a later source cannot discard
                # evidence discovered in an earlier one.
                unique_records = {
                    record.evidence_id_hash: record for record in session_records
                }
                records = sorted(
                    unique_records.values(),
                    key=lambda record: (
                        record.evidence_index,
                        record.observed_at,
                        record.evidence_id_hash,
                    ),
                )
                source_session = store.get(session_doc_id(session_id_hash))
                source_project = (
                    str(source_session.get("project") or "")
                    if source_session is not None
                    else ""
                )
                if source_project:
                    # Extractors do not own project resolution. When the source
                    # context is present, bind only missing record projects to
                    # its stored authority; a non-empty mismatch remains visible
                    # to the full-generation source-revision validation below.
                    records = [
                        replace(record, project=source_project)
                        if not record.project
                        else record
                        for record in records
                    ]
                # Re-enumerate immediately before replacement. The earlier
                # provider fence cannot see a same-session sibling that arrives
                # after it completes, and a selected source can disappear in
                # the final window. Either case can make this full generation
                # incomplete.
                final_paths = set(
                    enumerate_provider_files(provider, Path(root)) if root else []
                )
                changed_session_ids: set[str] = set()
                if final_paths != initial_paths:
                    try:
                        changed_session_ids = {
                            session_id_by_source_path[path]
                            for path in initial_paths - final_paths
                        }
                        changed_session_ids.update(
                            _tool_evidence_source_session_id(
                                provider=provider,
                                path=path,
                                runtime_dir=runtime_dir,
                            )
                            for path in final_paths - initial_paths
                        )
                    except Exception:  # noqa: BLE001 - unknown identity fails closed
                        has_unattributed_source_failure = True
                        prov["errors"] += 1
                        continue
                if session_id_hash in changed_session_ids or any(
                    _source_file_fingerprint(path)
                    != source_fingerprints.get(path)
                    for path in source_paths_by_session.get(session_id_hash, set())
                ):
                    incomplete_session_ids.add(session_id_hash)
                    prov["errors"] += 1
                    continue
                revs = store_tool_evidence_bundles(
                    records,
                    store=store,
                    full_session_generation=True,
                    session_id_hash=session_id_hash,
                )
                prov["bundles"] += len(revs)
                if records:
                    prov["sessions"] += 1
            except Exception:  # noqa: BLE001 - per-session fail-soft
                prov["errors"] += 1
        report["by_provider"][provider] = prov
        report["bundles"] += prov["bundles"]
        report["sessions_with_evidence"] += prov["sessions"]
        report["errors"] += prov["errors"]
    return report


def reconcile_coverage(store) -> dict:
    """Recompute every session's coverage_manifest from the chunks/bundles actually
    in the store. Multi-file sessions (same session_id_hash across several provider
    files) write per-file coverage that overwrites; this rebuilds authoritative
    coverage from the accumulated store so counts/hashes match reality.
    """
    sessions = store.find_by_type("transcript_session", fields=["session_id_hash"])
    sids = sorted({s["session_id_hash"] for s in sessions if s.get("session_id_hash")})
    reconciled = 0
    for sid in sids:
        update_coverage_with_tool_evidence(session_id_hash=sid, store=store)
        reconciled += 1
    return {"status": "ok", "reconciled": reconciled}


def load_corrective_locator_admission_manifest(
    path: str,
) -> tuple[dict[str, object], LocatorAdmission, str]:
    """Load a private, exact-shape admission manifest without exposing it."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != _CORRECTIVE_LOCATOR_ADMISSION_MANIFEST_FIELDS:
            raise ValueError
        locator = payload["locator"]
        admission_payload = payload["admission"]
        project = payload["project"]
        if (
            not isinstance(locator, dict)
            or not isinstance(admission_payload, dict)
            or set(admission_payload) != _LOCATOR_ADMISSION_FIELDS
            or not isinstance(project, str)
            or not project
        ):
            raise ValueError
        return dict(locator), LocatorAdmission(**admission_payload), project
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("corrective_import_manifest_invalid") from exc


def run_corrective_current_import(
    *,
    store,
    locator_manifest: dict[str, object],
    admission: LocatorAdmission,
    project: str,
) -> dict:
    """Admit and activate one immutable snapshot without a generic parser path."""
    try:
        snapshot = admit_codex_locator_snapshot(
            locator_manifest,
            admission,
            project=project,
        )
    except Exception:  # noqa: BLE001 - never expose private locator/admission detail
        snapshot = None

    result = None
    try:
        if snapshot is not None:
            result = activate_admitted_codex_current_source(snapshot=snapshot, store=store)
    except Exception:  # noqa: BLE001 - never expose private locator/admission detail
        result = None
    return {
        "dry_run": True,
        "found": 1,
        "admitted_candidates": int(snapshot is not None),
        "imported_current_revisions": int(result is not None and result.status == CURRENT_SOURCE_IMPORTED),
        "errors": int(result is None or result.status != CURRENT_SOURCE_IMPORTED),
        "mutation_performed": False,
        "network_used": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge transcript-migration")
    parser.add_argument("--provider", action="append", choices=list(MIGRATION_PROVIDERS))
    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "maximum source files for migration; with --tool-evidence, "
            "maximum complete sessions after stable source discovery"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approval", default="", help="Path to live-approval JSON (required for non-dry-run).")
    parser.add_argument("--source-root", action="append", help="provider=/path override; repeatable")
    parser.add_argument("--runtime-dir")
    parser.add_argument(
        "--locator-admission-manifest",
        default="",
        help="private bounded Codex locator/admission manifest; corrective dry-run only",
    )
    parser.add_argument("--reconcile-coverage", action="store_true", help="recompute coverage manifests from stored chunks and exit")
    parser.add_argument("--tool-evidence", action="store_true", help="second pass: store tool_evidence_bundle docs and exit")
    parser.add_argument(
        "--corrective-current-source",
        action="store_true",
        help="one Codex corrective current-source import; requires --dry-run and a private admission manifest",
    )
    args = parser.parse_args(argv if argv is not None else None)
    effective_argv = list(sys.argv[1:] if argv is None else argv)

    roots_override: dict[str, Path] = {}
    for raw in args.source_root or []:
        if "=" in raw:
            prov, _, p = raw.partition("=")
            roots_override[prov.strip()] = Path(p.strip()).expanduser()

    if args.corrective_current_source:
        if not args.dry_run:
            print(json.dumps({
                "status": "error",
                "error_class": "corrective_import_requires_dry_run",
                "dry_run": False,
                "mutation_performed": False,
                "network_used": False,
            }, sort_keys=True))
            return 2
        if args.source_root:
            print(json.dumps({
                "status": "error",
                "error_class": "corrective_import_rejects_source_root",
                "dry_run": True,
                "mutation_performed": False,
                "network_used": False,
            }, sort_keys=True))
            return 2
        if (
            args.provider
            or args.limit is not None
            or args.tool_evidence
            or args.reconcile_coverage
            or args.approval
            or args.runtime_dir
        ):
            print(json.dumps({
                "status": "error",
                "error_class": "corrective_import_rejects_legacy_target_flags",
                "dry_run": True,
                "mutation_performed": False,
                "network_used": False,
            }, sort_keys=True))
            return 2
        if not args.locator_admission_manifest:
            print(json.dumps({
                "status": "error",
                "error_class": "corrective_import_requires_locator_admission_manifest",
                "dry_run": True,
                "mutation_performed": False,
                "network_used": False,
            }, sort_keys=True))
            return 2
        try:
            locator_manifest, admission, project = load_corrective_locator_admission_manifest(
                args.locator_admission_manifest
            )
        except ValueError:
            print(json.dumps({
                "status": "error",
                "error_class": "corrective_import_manifest_invalid",
                "dry_run": True,
                "mutation_performed": False,
                "network_used": False,
            }, sort_keys=True))
            return 2
        report = run_corrective_current_import(
            store=InMemoryCouchDBSourceStore(),
            locator_manifest=locator_manifest,
            admission=admission,
            project=project,
        )
        report["status"] = "ok" if report["errors"] == 0 else "error"
        print(json.dumps(report, sort_keys=True))
        return 0 if report["errors"] == 0 else 1

    if args.tool_evidence:
        approval_error = _live_approval_error(args.approval, dry_run=args.dry_run, effective_argv=effective_argv)
        if approval_error is not None:
            print(json.dumps(approval_error, sort_keys=True))
            return 1
        store = InMemoryCouchDBSourceStore() if args.dry_run else build_store_from_env()
        roots = default_source_roots()
        roots.update(roots_override)
        runtime_dir = Path(args.runtime_dir) if args.runtime_dir else None
        if args.dry_run:
            # Full-generation evidence storage resolves the current source set,
            # including its transcript session and chunks. Seed that context only
            # in the disposable store, without limiting by source file: the tool
            # evidence limit applies later to complete selected sessions.
            run_migration(
                store=store,
                roots=roots,
                providers=args.provider,
                runtime_dir=runtime_dir,
                dry_run=True,
            )
        report = run_tool_evidence(
            store=store, roots=roots, providers=args.provider, limit=args.limit,
            runtime_dir=runtime_dir,
        )
        report["status"] = "ok" if report["errors"] == 0 else "error"
        print(json.dumps(report, sort_keys=True))
        return 0 if report["errors"] == 0 else 1

    if args.reconcile_coverage:
        approval_error = _live_approval_error(args.approval, dry_run=args.dry_run, effective_argv=effective_argv)
        if approval_error is not None:
            print(json.dumps(approval_error, sort_keys=True))
            return 1
        store = InMemoryCouchDBSourceStore() if args.dry_run else build_store_from_env()
        report = reconcile_coverage(store)
        print(json.dumps(report, sort_keys=True))
        return 0

    roots = default_source_roots()
    for raw in args.source_root or []:
        if "=" not in raw:
            print(json.dumps({"status": "error", "error_class": "bad_source_root"}))
            return 2
        prov, _, p = raw.partition("=")
        roots[prov.strip()] = Path(p.strip()).expanduser()

    approval_error = _live_approval_error(args.approval, dry_run=args.dry_run, effective_argv=effective_argv)
    if approval_error is not None:
        print(json.dumps(approval_error, sort_keys=True))
        return 1

    store = InMemoryCouchDBSourceStore() if args.dry_run else build_store_from_env()
    report = run_migration(
        store=store,
        roots=roots,
        providers=args.provider,
        limit=args.limit,
        runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
        dry_run=args.dry_run,
    )
    report["status"] = "ok"
    print(json.dumps(report, sort_keys=True))
    return 0


def _live_approval_error(approval: str, *, dry_run: bool, effective_argv: list[str]) -> dict | None:
    if dry_run:
        return None
    try:
        validate_memory_enqueue_approval(
            approval or None,
            operation=MIGRATION_CLI_OPERATION,
            command_argv=effective_argv,
        )
    except ApprovalError as exc:
        return {
            "schema_version": MIGRATION_CLI_SCHEMA_VERSION,
            "error": "approval_rejected",
            "reason": str(exc),
            "dry_run": False,
            "mutation_performed": False,
            "network_used": False,
        }
    return None


__all__ = [
    "MIGRATION_PROVIDERS",
    "default_source_roots",
    "enumerate_provider_files",
    "extract_cwd",
    "convert_gemini_json_to_fixture",
    "build_store_from_env",
    "run_migration",
    "load_corrective_locator_admission_manifest",
    "run_corrective_current_import",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
