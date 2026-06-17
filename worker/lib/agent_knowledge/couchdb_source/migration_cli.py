"""Live migration driver: Mac provider transcripts -> CouchDB source store.

Runs on the Mac (where the provider transcripts live; the Ubuntu server does not
read provider paths). For each provider it enumerates the on-disk session store,
extracts the session's own working directory (the authoritative ``project``
signal -- NOT RAGFlow's polluted metadata), and imports each session through the
``couchdb_source`` historical-import pipeline into a CouchDB store.

Provider roots (overridable):
  codex        ~/.codex/sessions/**/*.jsonl              (cwd from payload.cwd)
  claude       ~/.claude/projects/**/*.jsonl             (cwd from record.cwd)
  gemini       ~/.gemini/tmp/*/chats/*.{jsonl,json}      (.json -> fixture; project from <proj> dir)
  antigravity  ~/.gemini/antigravity/**/.system_generated/**/*.jsonl  (agy is captured here too)

The store target comes from env (COUCHDB_URL / COUCHDB_USER / COUCHDB_PASSWORD /
COUCHDB_DB); ``--dry-run`` uses an in-memory store so coverage and project
resolution can be inspected without any write.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from .couchdb_http_store import CouchDBHttpSourceStore
from .historical_import import ImportStatus, SourceLocator, import_historical_source
from .session_memory_materializer import update_coverage_with_tool_evidence
from .source_store import InMemoryCouchDBSourceStore
from .tool_evidence_bundler import store_tool_evidence_bundles
from .document_model import build_source_locator_hash
from ..session_memory.transcript_parsers import extract_tool_evidence

MIGRATION_PROVIDERS = ("codex", "claude", "gemini", "antigravity")
_CWD_SCAN_MAX_LINES = 50


def default_source_roots() -> dict[str, Path]:
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
    return {
        "codex": codex_home / "sessions",
        "claude": home / ".claude" / "projects",
        "gemini": home / ".gemini" / "tmp",
        "antigravity": home / ".gemini" / "antigravity",
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
    return sorted(p for p in root.glob("**/*.jsonl") if p.is_file() and not p.is_symlink())


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
                    capture_project = "antigravity" if provider == "antigravity" else gemini_project
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
                    if result.ragflow_project_mismatch:
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
    bounded tool_evidence_bundle docs in CouchDB. Idempotent (deterministic ids)."""
    roots = roots if roots is not None else default_source_roots()
    providers = providers or list(MIGRATION_PROVIDERS)
    runtime_dir = runtime_dir or (Path.home() / ".config" / "neurons" / "gemini-normalized")
    report: dict = {"by_provider": {}, "bundles": 0, "sessions_with_evidence": 0, "errors": 0}
    for provider in providers:
        root = roots.get(provider)
        files = enumerate_provider_files(provider, Path(root)) if root else []
        if limit is not None:
            files = files[: max(limit, 0)]
        prov = {"found": len(files), "bundles": 0, "sessions": 0, "errors": 0}
        for path in files:
            try:
                source_path = path
                if provider == "gemini" and path.suffix == ".json":
                    source_path = convert_gemini_json_to_fixture(path, runtime_dir)
                slh = build_source_locator_hash(str(source_path))
                records = extract_tool_evidence(provider, str(source_path), project="", source_locator_hash=slh)
                if not records:
                    continue
                revs = store_tool_evidence_bundles(records, store=store)
                prov["bundles"] += len(revs)
                prov["sessions"] += 1
            except Exception:  # noqa: BLE001 - per-file fail-soft
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge transcript-migration")
    parser.add_argument("--provider", action="append", choices=list(MIGRATION_PROVIDERS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-root", action="append", help="provider=/path override; repeatable")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--reconcile-coverage", action="store_true", help="recompute coverage manifests from stored chunks and exit")
    parser.add_argument("--tool-evidence", action="store_true", help="second pass: store tool_evidence_bundle docs and exit")
    args = parser.parse_args(argv if argv is not None else None)

    roots_override: dict[str, Path] = {}
    for raw in args.source_root or []:
        if "=" in raw:
            prov, _, p = raw.partition("=")
            roots_override[prov.strip()] = Path(p.strip()).expanduser()

    if args.tool_evidence:
        store = InMemoryCouchDBSourceStore() if args.dry_run else build_store_from_env()
        roots = default_source_roots()
        roots.update(roots_override)
        report = run_tool_evidence(
            store=store, roots=roots, providers=args.provider, limit=args.limit,
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
        )
        report["status"] = "ok"
        print(json.dumps(report, sort_keys=True))
        return 0

    if args.reconcile_coverage:
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


__all__ = [
    "MIGRATION_PROVIDERS",
    "default_source_roots",
    "enumerate_provider_files",
    "extract_cwd",
    "convert_gemini_json_to_fixture",
    "build_store_from_env",
    "run_migration",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
