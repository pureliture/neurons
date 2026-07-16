"""RetiredIndexBridge-fallback reconstruction for sessions whose provider originals are gone.

For sessions present in RetiredIndexBridge ``transcript-memory`` but absent from CouchDB
(originals rotated/deleted, or leak-blocked at re-import), reconstruct the
conversation source from RetiredIndexBridge's already-redacted copies. Every reconstructed
body is re-checked against the fail-closed leak gate; a session whose RetiredIndexBridge
copy still leaks is skipped (not stored). Reconstructed sessions are flagged
``source=index_fallback`` with an unverified project (RetiredIndexBridge project metadata
is polluted), so the retirement gate excludes them from clean-eligibility.

This is a recovery path, not the primary migration. Run it with RetiredIndexBridge + CouchDB
reachable (directly on the server, or via tunnels).
"""

from __future__ import annotations

import collections
import json
import urllib.request

from .couchdb_http_store import CouchDBHttpSourceStore
from .document_model import (
    SourceRedactionLeak,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_source_revision_token,
    build_transcript_session_document,
)
from ..session_memory.transcript_model import TranscriptChunk, TranscriptSession, canonicalize_project

RETIRED_INDEX_BRIDGE_FALLBACK_STATUS = "index_fallback"


class RetiredIndexBridgeReader:
    """Minimal RetiredIndexBridge HTTP reader (list documents + fetch chunk content)."""

    def __init__(self, *, base_url: str, api_key: str, dataset_id: str, timeout: float = 60):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.dataset_id = dataset_id
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        rq = urllib.request.Request(self.base + path, headers={"Authorization": "Bearer " + self.key})
        with urllib.request.urlopen(rq, timeout=self.timeout) as r:
            return json.loads(r.read())

    def session_doc_map(self, want: set[str], *, max_pages: int = 300) -> tuple[dict, dict]:
        """Map each wanted session_id_hash to its RetiredIndexBridge document ids + (provider, project)."""
        sid_docs: dict[str, list[str]] = collections.defaultdict(list)
        sid_meta: dict[str, tuple[str, str]] = {}
        page = 1
        while page <= max_pages:
            data = self._get(f"/api/v1/datasets/{self.dataset_id}/documents?page={page}&page_size=1000")
            docs = (data.get("data") or {}).get("docs") or []
            if not docs:
                break
            for doc in docs:
                mf = doc.get("meta_fields") or {}
                if not isinstance(mf, dict):
                    continue
                s = mf.get("session_id_hash", "")
                if s in want:
                    sid_docs[s].append(doc.get("id"))
                    sid_meta.setdefault(s, (mf.get("provider", ""), mf.get("project", "")))
            page += 1
        return sid_docs, sid_meta

    def doc_body(self, doc_id: str, *, max_chunks: int = 200) -> str:
        out: list[str] = []
        page = 1
        while True:
            data = self._get(
                f"/api/v1/datasets/{self.dataset_id}/documents/{doc_id}/chunks?page={page}&page_size=50"
            )
            d = data.get("data") or {}
            chunks = d.get("chunks") or d.get("chunk_list") or []
            if not chunks:
                break
            for c in chunks:
                txt = c.get("content") or c.get("content_with_weight") or ""
                if txt:
                    out.append(txt)
            if len(out) >= max_chunks or len(chunks) < 50:
                break
            page += 1
        return "\n".join(out).strip()


def reconstruct_sessions(
    *,
    session_hashes: list[str],
    reader: RetiredIndexBridgeReader,
    store: CouchDBHttpSourceStore,
) -> dict:
    want = set(session_hashes)
    sid_docs, sid_meta = reader.session_doc_map(want)
    report = {
        "requested": len(want),
        "found_in_retired_index_bridge": len(sid_docs),
        "reconstructed": 0,
        "leak_skipped": 0,
        "no_content": 0,
        "errors": 0,
        "chunks_written": 0,
    }
    for sid, doc_ids in sid_docs.items():
        provider, project_raw = sid_meta.get(sid, ("", ""))
        project = canonicalize_project(project_raw)
        try:
            bodies = [b for b in (reader.doc_body(d) for d in doc_ids if d) if b]
            if not bodies:
                report["no_content"] += 1
                continue
            chunk_docs = []
            content_hashes = []
            for i, body in enumerate(bodies):
                chunk = TranscriptChunk.from_text(
                    chunk_id="rfchunk_" + _hash16(body),
                    session_id_hash=sid,
                    provider=provider,
                    project=project,
                    turn_start_index=i,
                    turn_end_index=i,
                    text=body,
                    source_status=RETIRED_INDEX_BRIDGE_FALLBACK_STATUS,
                )
                doc = build_conversation_chunk_document(chunk=chunk)  # fail-closed leak gate inside
                chunk_docs.append(doc)
                content_hashes.append(doc["content_hash"])
            session = TranscriptSession(
                session_id_hash=sid, provider=provider, project=project,
                started_at="", source_status=RETIRED_INDEX_BRIDGE_FALLBACK_STATUS,
            )
            store.put(build_transcript_session_document(session=session))
            for doc in chunk_docs:
                store.put(doc)
            store.put(build_coverage_manifest_document(
                session_id_hash=sid, provider=provider, project=project,
                conversation_chunk_count=len(chunk_docs), tool_evidence_bundle_count=0,
                conversation_content_hashes=content_hashes, tool_evidence_coverage_hashes=[],
                conversation_revision_tokens=[
                    build_source_revision_token(doc, material_hash_field="content_hash")
                    for doc in chunk_docs
                ],
                project_authority={
                    "project": project, "source": RETIRED_INDEX_BRIDGE_FALLBACK_STATUS, "ambiguous": True,
                    "eligible_for_retirement": False,
                    "notes": ["index_fallback", "index_project_unverified"],
                },
            ))
            report["reconstructed"] += 1
            report["chunks_written"] += len(chunk_docs)
        except SourceRedactionLeak:
            report["leak_skipped"] += 1
        except Exception:  # noqa: BLE001 - per-session fail-soft
            report["errors"] += 1
    return report


def _hash16(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


__all__ = ["RetiredIndexBridgeReader", "reconstruct_sessions", "RETIRED_INDEX_BRIDGE_FALLBACK_STATUS"]
