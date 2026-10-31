"""기존 immutable Couch source에서 native 관측을 정확히 admission한다.

source의 active version과 native capture identity를 확인하는 경로다.
세션의 과거 호출을 해당 사실의 최신 값으로 인증하는 경로가 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from . import document_model as dm
from .source_revision import resolve_active_source_revision, SourceRevisionResolutionError
from .source_store import SourceStoreError
from .couchdb_http_store import CouchDBError
from ..session_memory.observation_refs import HASH, VERSION, validate
from ..session_memory.transcript_model import REDACTION_VERSION


class ObservationUnavailable(ValueError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class AdmittedNativeObservation:
    project: str
    provider: str
    session_id_hash: str
    source_hash: str
    redacted_text: str
    _reference_json: str
    _aliases_json: str
    _native_receipt_json: str = ""

    @property
    def reference(self):
        return json.loads(self._reference_json)

    def public_proof(self):
        proof = {"adapter": "couch_native_observation.v1", "project": self.project, "provider": self.provider,
            "session_id_hash": self.session_id_hash, "source_hash": self.source_hash,
            "reference": self.reference, "source_aliases": json.loads(self._aliases_json)}
        if self._native_receipt_json:
            proof["native_receipt_hash"] = self.native_receipt["receipt_hash"]
        return proof

    @property
    def native_receipt(self):
        return json.loads(self._native_receipt_json) if self._native_receipt_json else None


class CouchObservationCatalog:
    def __init__(self, store, *, extractor_versions, redaction_version=REDACTION_VERSION):
        if (not isinstance(extractor_versions, dict) or not extractor_versions
                or any(not isinstance(key, str) or not isinstance(value, str) or value == "unknown"
                    or not VERSION.fullmatch(value) for key, value in extractor_versions.items())):
            raise ValueError("explicit current extractor policy required")
        if not isinstance(redaction_version, str) or not VERSION.fullmatch(redaction_version):
            raise ValueError("explicit redaction policy required")
        self.store = store
        self.extractor_versions = dict(extractor_versions)
        self.redaction_version = redaction_version

    def _resolve(self, session_id_hash):
        try:
            source = resolve_active_source_revision(store=self.store, session_id_hash=session_id_hash)
        except (SourceStoreError, CouchDBError, SourceRevisionResolutionError, ValueError, KeyError, TypeError):
            raise ObservationUnavailable("observation_source_unavailable") from None
        if source.is_legacy_unpinned or not source.manifest_id:
            raise ObservationUnavailable("observation_source_unpinned")
        return source

    def read_many(self, *, project, provider, session_id_hash, source_hash, evidence_id_hashes):
        if (not isinstance(evidence_id_hashes, list) or not 1 <= len(evidence_id_hashes) <= 100
                or any(not isinstance(key, str) for key in evidence_id_hashes)
                or len(set(evidence_id_hashes)) != len(evidence_id_hashes)):
            raise ValueError("bounded unique native evidence identities required")
        for value in (session_id_hash, source_hash, *evidence_id_hashes):
            if not isinstance(value, str) or not HASH.fullmatch(value):
                raise ValueError("exact source/evidence digest required")
        source = self._resolve(session_id_hash)
        if source.source_hash != source_hash:
            raise ObservationUnavailable("observation_source_revision_conflict")
        if any((document.get("project"), document.get("provider")) != (project, provider)
                for document in (*source.sessions, *source.conversation_chunks, *source.tool_evidence_bundles)):
            raise ObservationUnavailable("observation_scope_mismatch")
        wanted = set(evidence_id_hashes)
        found = {}
        for bundle in source.tool_evidence_bundles:
            if (bundle.get("project"), bundle.get("provider")) != (project, provider):
                raise ObservationUnavailable("observation_scope_mismatch")
            body = bundle.get("body", "")
            try:
                references = validate(bundle.get("observation_refs", []), body=body,
                    session_id_hash=session_id_hash, provider=provider, project=project)
                from ..session_memory.native_tool_receipts import from_bundle
                native_receipts = from_bundle(bundle,references)
            except (ValueError, TypeError, KeyError):
                raise ObservationUnavailable("observation_source_integrity_invalid") from None
            for ref in references:
                evidence_id = ref["evidence_id_hash"]
                if evidence_id not in wanted:
                    continue
                if not ref["observation_id_hash"]:
                    raise ObservationUnavailable("native_observation_anchor_required")
                if self.extractor_versions.get(provider) != ref["extractor_version"]:
                    raise ObservationUnavailable("observation_extractor_policy_mismatch")
                if self.redaction_version != ref["redaction_version"]:
                    raise ObservationUnavailable("observation_redaction_policy_mismatch")
                text = body[ref["span_start"]:ref["span_end"]]
                signature = {key: value for key, value in ref.items()
                    if key not in {"span_start", "span_end", "evidence_index", "source_event_index"}}
                alias = {"source_document_hash": dm.sha256_hash(bundle["_id"]),
                    "span_start": ref["span_start"], "span_end": ref["span_end"],
                    "span_content_hash": ref["span_content_hash"]}
                if evidence_id in native_receipts:
                    alias["source_event_index"] = ref["source_event_index"]
                previous = found.get(evidence_id)
                if previous:
                    if (previous["signature"] != signature or previous["text"] != text
                            or previous["native_receipt"] != native_receipts.get(evidence_id)):
                        raise ObservationUnavailable("observation_alias_conflict")
                    previous["aliases"].append(alias)
                else:
                    found[evidence_id] = {"reference": ref, "signature": signature, "text": text,
                        "native_receipt":native_receipts.get(evidence_id), "aliases": [alias]}
        if wanted != set(found):
            raise ObservationUnavailable("native_observation_not_found")
        # 도착 시각으로 승자를 정하지 않고 같은 source revision을 재확인한다.
        if self._resolve(session_id_hash).source_hash != source_hash:
            raise ObservationUnavailable("observation_source_changed_during_admission")
        return {key: AdmittedNativeObservation(project, provider, session_id_hash, source_hash, value["text"],
            json.dumps(value["reference"], sort_keys=True), json.dumps(value["aliases"], sort_keys=True),
            json.dumps(value["native_receipt"],sort_keys=True) if value["native_receipt"] else "")
            for key, value in found.items()}

    def read_tool_result(self, *, project, provider, session_id_hash, source_hash, evidence_id_hash,
                         require_complete=True):
        """실제 저장된 native 결과를 읽는다. latest 세계 상태라는 capability는 없다."""
        admitted=self.read_many(project=project,provider=provider,session_id_hash=session_id_hash,
            source_hash=source_hash,evidence_id_hashes=[evidence_id_hash])[evidence_id_hash]
        receipt=admitted.native_receipt
        if receipt is None:
            raise ObservationUnavailable("native_tool_receipt_required")
        if require_complete and not (receipt["arguments_complete"] and receipt["result_complete"]):
            raise ObservationUnavailable("native_tool_receipt_incomplete")
        return admitted

    def assert_current(self, observation):
        if not isinstance(observation, AdmittedNativeObservation):
            raise ValueError("admitted native observation required")
        refreshed = self.read_many(project=observation.project, provider=observation.provider,
            session_id_hash=observation.session_id_hash, source_hash=observation.source_hash,
            evidence_id_hashes=[observation.reference["evidence_id_hash"]])
        if refreshed[observation.reference["evidence_id_hash"]].public_proof() != observation.public_proof():
            raise ObservationUnavailable("observation_source_changed_during_admission")

    def read_projected_chunk(self, *, project, provider, session_id_hash, chunk_id, sql_store):
        """PG의 최신 도착 행 대신 현재 Couch source와 정확히 연결된 본문을 읽는다.

        PG representation은 마스킹된 본문일 수 있으므로 native span 좌표는
        기존 canonical materialization에서 읽는다. 새로운 본문 저장소는 만들지 않는다.
        """
        from .session_memory_materializer import materialize_session_memory
        source = self._resolve(session_id_hash)
        try:
            materialized = materialize_session_memory(session_id_hash=session_id_hash, store=self.store)
        except (SourceStoreError, CouchDBError, SourceRevisionResolutionError, ValueError, KeyError, TypeError):
            raise ObservationUnavailable("observation_materialization_unavailable") from None
        if (not materialized.fully_materialized or materialized.source_hash != source.source_hash
                or (materialized.project, materialized.provider) != (project, provider)):
            raise ObservationUnavailable("observation_scope_or_materialization_mismatch")
        links = sql_store.get_chunk_source_provenance(project=project, chunk_ids=[chunk_id])
        matching = [link for link in links if link["source_hash"] == source.source_hash]
        if len(matching) != 1:
            raise ObservationUnavailable("observation_projection_source_missing")
        link = matching[0]
        row = sql_store.get_chunk(chunk_id)
        if (row is None or (row.project, row.provider, row.session_id_hash) != (project, provider, session_id_hash)
                or row.content_hash != link["representation_content_hash"]
                or dm.sha256_hash(row.content_markdown) != row.content_hash):
            raise ObservationUnavailable("observation_projection_integrity_mismatch")
        references = list(materialized.observation_refs)
        if ((link["session_id_hash"], link["provider"], link["project"]) != (session_id_hash, provider, project)
                or link["source_content_hash"] != materialized.content_hash
                or link["observation_refs"] != references):
            raise ObservationUnavailable("observation_projection_provenance_mismatch")
        ids = list(dict.fromkeys(ref["evidence_id_hash"] for ref in references))
        for start in range(0, len(ids), 100):
            self.read_many(project=project, provider=provider, session_id_hash=session_id_hash,
                source_hash=source.source_hash, evidence_id_hashes=ids[start:start + 100])
        if self._resolve(session_id_hash).source_hash != source.source_hash:
            raise ObservationUnavailable("observation_source_changed_during_admission")
        return {"content": materialized.body, "content_hash": materialized.content_hash,
            "source_hash": source.source_hash, "observation_refs": references,
            "session_id_hash": session_id_hash, "project": project, "provider": provider,
            "metadata": {"knowledge_id": chunk_id, "project": project, "provider": provider,
                "source_admission": "couch_native_observation.v1"}}
