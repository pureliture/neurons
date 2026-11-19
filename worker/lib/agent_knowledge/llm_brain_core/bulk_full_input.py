"""Isolated trial: full-input chunk map, policy-aware resume, shadow isolation.

Synthetic/offline only. This module is a candidate implementation for review;
it is not deployed and does not connect to production DB, K8s, or any API.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from pathlib import Path
from types import SimpleNamespace
from .graph_scope import graph_group_id, graph_group_ids
from .runtime import (
    _conversation_chunk_order_key,
    _strip_chunk_metadata_header,
    session_source_revision_from_couchdb_source,
)

_SEPARATOR = b"\n"
_SHADOW_PREFIX = "shadow_"


class SourceInvalid(ValueError):
    """Member ordering/coordinates conflict; never silently reordered."""


class FencingRejected(RuntimeError):
    """A stale lease attempted to finish a chunk."""


class DuplicateRejected(RuntimeError):
    """A chunk key was re-completed with a different verified payload."""


class PaidModelRejected(RuntimeError):
    """Candidate is not a confirmed-free approved model/endpoint."""


class ProviderIdentityRejected(RuntimeError):
    """Runtime provider/origin does not match the authorized free model lane."""


class PrivacyGateRejected(RuntimeError):
    """Canonical input failed the public-safe boundary."""

@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int


@dataclass(frozen=True)
class ChunkPlan:
    lane: str
    ordinal: int
    member_hashes: tuple[str, ...]
    basic_ranges: tuple[ByteRange, ...]
    overlap_ranges: tuple[ByteRange, ...]
    canonical_hash: str

    @property
    def key(self) -> str:
        payload = json.dumps(
            [self.lane, self.ordinal, list(self.member_hashes),
             [[r.start, r.end] for r in self.basic_ranges],
             [[r.start, r.end] for r in self.overlap_ranges],
             self.canonical_hash],
            separators=(",", ":"),
        ).encode("utf-8")
        return _digest(payload)


@dataclass(frozen=True)
class SessionPlan:
    run_id: str
    session_hash: str
    source_hash: str
    policy_version: str
    chunks: tuple[ChunkPlan, ...]
    total_bytes: int
    map_digest: str


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _utf8_start(data: bytes, candidate: int) -> int:
    """Move a byte candidate forward to the next UTF-8 character boundary."""
    while candidate < len(data) and (data[candidate] & 0xC0) == 0x80:
        candidate += 1
    return candidate


class FullInputBuilder:
    """Build a byte-addressed canonical input and its chunk map.

    Bodies stay in the builder for this synthetic trial; the design's bounded
    memory/paging claim is deliberately NOT validated here.
    """

    def __init__(
        self,
        documents: Iterable[Mapping[str, Any]],
        *,
        session_hash: str,
        source_hash: str,
        policy_version: str,
        run_id: str,
        lane: str = "conversation",
        overlap_bytes: int = 0,
        evidence_documents: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        ordered = sorted(documents, key=_conversation_chunk_order_key)
        seen: dict[tuple, bytes] = {}
        bodies: list[bytes] = []
        hashes: list[str] = []
        for doc in ordered:
            body = _strip_chunk_metadata_header(str(doc.get("body") or "")).encode("utf-8")
            order_key = _conversation_chunk_order_key(doc)
            if order_key in seen and seen[order_key] != body:
                raise SourceInvalid("conflicting bodies at identical member coordinates")
            if order_key in seen:
                continue
            seen[order_key] = body
            bodies.append(body)
            hashes.append(_digest(body))

        canonical = bytearray()
        ranges: list[ByteRange] = []
        data_starts: list[int] = []
        for index, body in enumerate(bodies):
            if index:
                canonical.extend(_SEPARATOR)
            data_start = len(canonical)
            canonical.extend(body)
            data_starts.append(data_start)
            range_start = data_start - len(_SEPARATOR) if index else 0
            ranges.append(ByteRange(range_start, len(canonical)))

        self.canonical_bytes = bytes(canonical)
        chunks: list[ChunkPlan] = []
        for index, body in enumerate(bodies):
            overlap: tuple[ByteRange, ...] = ()
            if overlap_bytes and index:
                previous = bodies[index - 1]
                start = _utf8_start(previous, max(0, len(previous) - overlap_bytes))
                previous_start = data_starts[index - 1]
                overlap = (ByteRange(previous_start + start, previous_start + len(previous)),)
            chunks.append(
                ChunkPlan(
                    lane=lane,
                    ordinal=index,
                    member_hashes=(hashes[index],),
                    basic_ranges=(ranges[index],),
                    overlap_ranges=overlap,
                    canonical_hash=_digest(body),
                )
            )
        self.plan = SessionPlan(
            run_id=run_id,
            session_hash=session_hash,
            source_hash=source_hash,
            policy_version=policy_version,
            chunks=tuple(chunks),
            total_bytes=len(self.canonical_bytes),
            map_digest=_digest(
                json.dumps(
                    [run_id, session_hash, source_hash, policy_version,
                     [chunk.key for chunk in chunks], len(self.canonical_bytes)],
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )

        self._evidence_bodies = [
            str(doc.get("body") or "").encode("utf-8") for doc in evidence_documents
        ]
        self.evidence_plan = SessionPlan(
            run_id=run_id,
            session_hash=session_hash,
            source_hash=source_hash,
            policy_version=policy_version,
            chunks=tuple(
                ChunkPlan(
                    lane="evidence",
                    ordinal=index,
                    member_hashes=(_digest(body),),
                    basic_ranges=(ByteRange(0, len(body)),),
                    overlap_ranges=(),
                    canonical_hash=_digest(body),
                )
                for index, body in enumerate(self._evidence_bodies)
            ),
            total_bytes=sum(len(body) for body in self._evidence_bodies),
            map_digest=_digest(
                json.dumps([_digest(body) for body in self._evidence_bodies]).encode("utf-8")
            ),
        )

    def render(self, ordinal: int) -> bytes:
        """Return overlap context plus this chunk's owned basic bytes."""
        chunk = self.plan.chunks[ordinal]
        pieces = [self.canonical_bytes[r.start:r.end]
                  for r in (*chunk.overlap_ranges, *chunk.basic_ranges)]
        return b"".join(pieces)

    def render_evidence(self, ordinal: int) -> str:
        return self._evidence_bodies[ordinal].decode("utf-8")


def needs_reprocess(
    *,
    completed_policy_version: str,
    completed_source_hash: str,
    completed_map_digest: str,
    planned_policy_version: str,
    planned_source_hash: str,
    planned_map_digest: str,
    all_chunks_verified: bool,
) -> bool:
    """Old completion rows never suppress a new policy or partial coverage."""
    return not (
        all_chunks_verified
        and completed_policy_version == planned_policy_version
        and completed_source_hash == planned_source_hash
        and completed_map_digest == planned_map_digest
    )


def unaffected_ordinals(old: SessionPlan, new: SessionPlan) -> tuple[int, ...]:
    """Reuse only the unchanged prefix; any earlier edit affects later chunks."""
    by_ordinal = {chunk.ordinal: chunk for chunk in new.chunks}
    result: list[int] = []
    for chunk in old.chunks:
        candidate = by_ordinal.get(chunk.ordinal)
        if candidate is not None and candidate == chunk and len(result) == chunk.ordinal:
            result.append(chunk.ordinal)
        else:
            break
    return tuple(result)


class InMemoryChunkState:
    """Minimal PG-like state seam with lease tokens and write receipts."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def claim(self, key: str, *, owner: str, lease_expiry: int) -> int:
        record = self._records.setdefault(
            key, {"token": 0, "owner": "", "expiry": -1, "digest": None,
                  "receipt": None, "complete": False, "writes": 0}
        )
        record["token"] += 1
        record["owner"] = owner
        record["expiry"] = lease_expiry
        return record["token"]

    def complete(self, key: str, *, token: int, output_digest: str, write_receipt: str) -> None:
        record = self._records.get(key)
        if record is None or token != record["token"]:
            raise FencingRejected("stale lease token")
        if record["complete"]:
            if record["digest"] == output_digest and record["receipt"] == write_receipt:
                return
            raise DuplicateRejected("chunk key already completed with another payload")
        record["digest"] = output_digest
        record["receipt"] = write_receipt
        record["complete"] = True
        record["writes"] += 1

    def is_complete(self, key: str) -> bool:
        record = self._records.get(key)
        return bool(record and record["complete"] and record["receipt"])

    def all_verified(self, keys: Iterable[str]) -> bool:
        keys = tuple(keys)
        return bool(keys) and all(self.is_complete(key) for key in keys)

    def write_count(self, key: str) -> int:
        record = self._records.get(key)
        return int(record["writes"]) if record else 0


def shadow_group_ids(scope: str, policy_version: str) -> tuple[str, ...]:
    """Derive a versioned shadow group disjoint from current/legacy groups."""
    base = graph_group_id(scope)
    if not base:
        return ()
    digest = hashlib.sha256(f"{base}|{policy_version}".encode("utf-8")).hexdigest()[:16]
    return (f"{_SHADOW_PREFIX}{digest}",)


def is_shadow_group(group_id: str) -> bool:
    return str(group_id).startswith(_SHADOW_PREFIX)


def eligible_reader_group_ids(scope: str, policy_version: str, *, eligible: bool) -> tuple[str, ...]:
    """Shadow groups stay invisible until explicitly made eligible."""
    base = graph_group_ids(scope)
    if not eligible:
        return base
    return tuple(dict.fromkeys((*base, *shadow_group_ids(scope, policy_version))))


@dataclass(frozen=True)
class ModelPolicy:
    provider: str
    model: str
    endpoint_class: str
    price_status: str


def assert_free_only(policy: ModelPolicy, allowlist: frozenset[str]) -> None:
    if (
        policy.model not in allowlist
        or policy.endpoint_class != "approved_free"
        or policy.price_status != "free_confirmed"
    ):
        raise PaidModelRejected("model/endpoint is not confirmed free and approved")


def choose_fallback(
    candidates: Iterable[ModelPolicy], allowlist: frozenset[str]
) -> ModelPolicy | None:
    for policy in candidates:
        try:
            assert_free_only(policy, allowlist)
        except PaidModelRejected:
            continue
        return policy
    return None


def privacy_gate(
    canonical: bytes, *, forbidden_markers: tuple[bytes, ...], all_redacted: bool
) -> None:
    if not all_redacted or any(marker in canonical for marker in forbidden_markers):
        raise PrivacyGateRejected("canonical input failed public-safe gate")


class ChunkTooLarge(RuntimeError):
    """Canonical chunk exceeds the configured byte budget; re-split the map."""


def make_full_chunk_session_input(
    *,
    builder: FullInputBuilder,
    ordinal: int,
    max_bytes: int,
    forbidden_markers: tuple[bytes, ...] = (),
    all_redacted: bool = True,
) -> str:
    """Render one whole canonical chunk, never head/tail compressed.

    This is the trial replacement for the 8,000-char pre-clip plus the bulk
    head/tail compaction. Oversize input fails closed so the map must be
    re-split with a new version instead of silently dropping bytes.
    """
    privacy_gate(
        builder.canonical_bytes,
        forbidden_markers=forbidden_markers,
        all_redacted=all_redacted,
    )
    data = builder.render(ordinal)
    if len(data) > max_bytes:
        raise ChunkTooLarge("canonical chunk exceeds byte budget; re-split required")
    return data.decode("utf-8")


class FullInputState:
    """File-backed trial receipts, keyed by chunk key under a runtime dir."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.path = Path(runtime_dir) / "full-input-state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._data = dict(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, sort_keys=True), encoding="utf-8")

    def record_chunk(
        self,
        *,
        key: str,
        project: str,
        policy_version: str,
        source_hash: str,
        map_digest: str,
        output_digest: str,
        receipt: str,
    ) -> None:
        self._data[key] = {
            "project": project,
            "policy_version": policy_version,
            "source_hash": source_hash,
            "map_digest": map_digest,
            "output_digest": output_digest,
            "receipt": receipt,
        }
        self._save()


    def is_complete(self, key: str) -> bool:
        record = self._data.get(key)
        return bool(record and record.get("receipt"))
    def completed_chunks(self, project: str, policy_version: str) -> list[str]:
        return [
            key for key, record in self._data.items()
            if record.get("project") == project
            and record.get("policy_version") == policy_version
        ]

    def is_session_complete(
        self,
        *,
        project: str,
        policy_version: str,
        source_hash: str,
        map_digest: str,
        chunk_keys: Iterable[str],
    ) -> bool:
        keys = tuple(chunk_keys)
        if not keys:
            return False
        for key in keys:
            record = self._data.get(key)
            if record is None or not record.get("receipt"):
                return False
            if (
                record.get("project") != project
                or record.get("policy_version") != policy_version
                or record.get("source_hash") != source_hash
                or record.get("map_digest") != map_digest
            ):
                return False
        return True


def validate_source_documents(documents: Iterable[Mapping[str, Any]], manifest: Mapping[str, Any]) -> None:
    """Fail closed when declared coverage does not match available members."""
    docs = list(documents)
    chunks = sum(1 for doc in docs if doc.get("doc_type") == "conversation_chunk")
    evidence = sum(1 for doc in docs if doc.get("doc_type") == "tool_evidence_bundle")
    if chunks != int(manifest.get("conversation_chunk_count") or 0):
        raise SourceInvalid("declared conversation chunk coverage does not match stored members")
    if evidence != int(manifest.get("tool_evidence_bundle_count") or 0):
        raise SourceInvalid("declared evidence bundle coverage does not match stored members")


def enforce_free_only_extractor(extractor: Any, allowlist: frozenset[str]) -> None:
    """Fail closed unless the injected extractor declares confirmed-free policy."""
    policy = ModelPolicy(
        provider=str(getattr(extractor, "provider", "")),
        model=str(getattr(extractor, "model", "")),
        endpoint_class=str(getattr(extractor, "endpoint_class", "")),
        price_status=str(getattr(extractor, "price_status", "")),
    )
    assert_free_only(policy, allowlist)



def resolve_trial_pg_dsn(environ: Mapping[str, str] | None = None) -> str:
    """Resolve trial PG DSN with explicit TRIAL_PG_DSN overriding ops NEURON_LEDGER_PG_DSN."""
    env = environ if environ is not None else os.environ
    for key in ("TRIAL_PG_DSN", "NEURON_LEDGER_PG_DSN"):
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return ""


def resolve_trial_neo4j_config(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """Resolve trial Neo4j (uri, user, password), preferring TRIAL_* over LLM_BRAIN_*."""
    env = environ if environ is not None else os.environ

    def _pick(*keys: str) -> str:
        for key in keys:
            value = str(env.get(key) or "").strip()
            if value:
                return value
        return ""

    uri = _pick("TRIAL_NEO4J_URI", "LLM_BRAIN_NEO4J_URI")
    user = _pick("TRIAL_NEO4J_USER", "LLM_BRAIN_NEO4J_USER") or "neo4j"
    password = _pick("TRIAL_NEO4J_PASSWORD", "LLM_BRAIN_NEO4J_PASSWORD")
    missing: list[str] = []
    if not uri:
        missing.append("TRIAL_NEO4J_URI/LLM_BRAIN_NEO4J_URI")
    if not password:
        missing.append("TRIAL_NEO4J_PASSWORD/LLM_BRAIN_NEO4J_PASSWORD")
    if missing:
        raise RuntimeError(f"missing trial Neo4j config env: {', '.join(missing)}")
    return uri, user, password


def enforce_opencode_go_hy4_flash_free(
    extractor: Any,
    *,
    allowlist: frozenset[str],
) -> None:
    """Require the authorized OpenCode Go runtime, not merely a model name."""
    from agent_knowledge.model_connectors.session_headers import is_opencode_go_openai_base_url

    base_url = str(getattr(extractor, "_base_url", "") or "")
    model = str(getattr(extractor, "model", getattr(extractor, "_model", "")) or "")
    if not is_opencode_go_openai_base_url(base_url):
        raise ProviderIdentityRejected("runtime provider/origin is not OpenCode Go")
    if model != "hy4-flash-free" or model not in allowlist:
        raise PaidModelRejected("runtime model is not the authorized free model")
    enforce_free_only_extractor(extractor, allowlist)

def build_session_grounding_fact(
    *,
    project: str,
    memory_id: str,
    content_hash: str,
    shadow_group: str,
    chunk_key: str,
) -> Any:
    """Make a card-backed Fact so real PG-authority join can expose session grounding."""
    return SimpleNamespace(
        entity_type="GraphFact",
        payload={
            "authority_sources": [
                {"authority_memory_id": memory_id, "content_hash": content_hash}
            ],
            "source_entity_types": ["Entity"],
            "project": project,
            "shadow_group": shadow_group,
            "chunk_key": chunk_key,
        },
        valid_from=None,
        valid_to=None,
        currentness=None,
    )


def iter_bounded_chunks(
    documents: Iterable[Mapping[str, Any]], *, max_resident_bodies: int = 2
) -> Iterable[tuple[int, bytes]]:
    """Yield one canonical member body at a time without materializing all bodies."""
    if max_resident_bodies < 1:
        raise ValueError("max_resident_bodies must be positive")
    ordered = sorted(documents, key=_conversation_chunk_order_key)
    seen: dict[tuple, bytes] = {}
    ordinal = 0
    for doc in ordered:
        key = _conversation_chunk_order_key(doc)
        body = _strip_chunk_metadata_header(str(doc.get("body") or "")).encode("utf-8")
        if key in seen:
            if seen[key] != body:
                raise SourceInvalid("conflicting bodies at identical member coordinates")
            continue
        seen[key] = body
        if len(seen) > max_resident_bodies:
            seen.pop(next(iter(seen)))
        yield ordinal, body
        ordinal += 1


def assert_source_current(
    *, session_id_hash: str, source_store: Any, expected_hash: str
) -> None:
    current = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash, source_store=source_store
    )
    if str(current) != str(expected_hash):
        raise SourceInvalid("source revision changed during run")
