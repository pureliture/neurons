"""Local Model2Vec profile helpers for the vendored graphiti_core 0.30.1 fork."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
import re
from typing import Any, Iterator
import unicodedata

LOCAL_GRAPH_PROFILE_ID = "lbrain-memory-model2vec-256-v1"
LOCAL_GRAPH_MODEL_ALIASES = frozenset(
    {
        "local-model2vec-256-v1",
        "minishlab/potion-multilingual-128m",
        "lbrain-memory-model2vec-256-v1",
    }
)
LOCAL_GRAPH_VECTOR_DIM = 256

NODE_LEGACY_EMBEDDING_PROP = "name_embedding"
NODE_LOCAL_EMBEDDING_PROP = "name_embedding_model2vec_v1"
NODE_LOCAL_PROFILE_PROP = "name_embedding_model2vec_v1_profile_id"
NODE_LOCAL_INPUT_HASH_PROP = "name_embedding_model2vec_v1_input_hash"

EDGE_LEGACY_EMBEDDING_PROP = "fact_embedding"
EDGE_LOCAL_EMBEDDING_PROP = "fact_embedding_model2vec_v1"
EDGE_LOCAL_PROFILE_PROP = "fact_embedding_model2vec_v1_profile_id"
EDGE_LOCAL_INPUT_HASH_PROP = "fact_embedding_model2vec_v1_input_hash"

GRAPH_EMBEDDING_DIRTY_PROP = "embedding_dirty"
GRAPH_TRANSIENT_CAS_LOCK_PROP = "_transient_cas_lock"

INTERNAL_GRAPH_METADATA_PROPS = frozenset(
    {
        NODE_LOCAL_EMBEDDING_PROP,
        NODE_LOCAL_PROFILE_PROP,
        NODE_LOCAL_INPUT_HASH_PROP,
        EDGE_LOCAL_EMBEDDING_PROP,
        EDGE_LOCAL_PROFILE_PROP,
        EDGE_LOCAL_INPUT_HASH_PROP,
        GRAPH_EMBEDDING_DIRTY_PROP,
        GRAPH_TRANSIENT_CAS_LOCK_PROP,
        "local_name_embedding",
        "local_fact_embedding",
        "incarnation",
    }
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOCAL_EMBEDDING_MODE_VAR: ContextVar[bool | None] = ContextVar(
    "graphiti_local_embedding_mode", default=None
)


class CommunityDisabledInLocalModeError(RuntimeError):
    """Raised when any Graphiti Community operation is invoked in local Model2Vec mode."""


@contextmanager
def local_embedding_mode_context(enabled: bool = True) -> Iterator[None]:
    """Explicitly set local Model2Vec mode in the current execution context."""
    token = _LOCAL_EMBEDDING_MODE_VAR.set(bool(enabled))
    try:
        yield
    finally:
        _LOCAL_EMBEDDING_MODE_VAR.reset(token)


def _object_indicates_local_mode(obj: Any) -> bool:
    if obj is None:
        return False
    profile = str(
        getattr(obj, "local_embedding_profile", None)
        or getattr(obj, "profile_id", None)
        or ""
    ).strip().lower()
    if profile in LOCAL_GRAPH_MODEL_ALIASES:
        return True
    config = getattr(obj, "config", None)
    if config is not None:
        model = str(getattr(config, "embedding_model", "") or "").strip().lower()
        if model in LOCAL_GRAPH_MODEL_ALIASES:
            return True
        dim = getattr(config, "embedding_dim", None)
        if dim == LOCAL_GRAPH_VECTOR_DIM:
            return True
    inner_client = getattr(obj, "_embedder", None)
    if inner_client is not None and inner_client is not obj:
        if _object_indicates_local_mode(inner_client):
            return True
    return False


def is_local_embedding_mode(*objects: Any, environ: dict[str, str] | None = None) -> bool:
    """Return True when Graphiti should use the 256-dim local Model2Vec property contract."""
    ctx_override = _LOCAL_EMBEDDING_MODE_VAR.get()
    if ctx_override is not None:
        return ctx_override

    for obj in objects:
        if _object_indicates_local_mode(obj):
            return True
        for attr in ("driver", "embedder", "clients"):
            nested = getattr(obj, attr, None)
            if nested is not None and nested is not obj and _object_indicates_local_mode(nested):
                return True

    env = environ if environ is not None else os.environ
    for key in (
        "NEURON_EMBEDDING_PROFILE",
        "LLM_BRAIN_EMBEDDING_PROFILE",
        "LLM_BRAIN_EMBEDDING_MODEL",
        "EMBEDDING_MODEL",
    ):
        val = str(env.get(key, "") or "").strip().lower()
        if val in LOCAL_GRAPH_MODEL_ALIASES:
            return True
    dim_raw = str(env.get("LLM_BRAIN_EMBEDDING_DIM", "") or "").strip()
    if dim_raw == str(LOCAL_GRAPH_VECTOR_DIM):
        return True
    return False


def ensure_community_allowed(*objects: Any) -> None:
    """Fail closed if a Community operation is called while local Model2Vec mode is active."""
    if is_local_embedding_mode(*objects):
        raise CommunityDisabledInLocalModeError(
            f"Graphiti Community operations are disabled under local profile {LOCAL_GRAPH_PROFILE_ID}"
        )


def normalize_canonical_text_v1(text: str) -> str:
    """Normalize text identically to canonical_embedding_v1."""
    normalized = unicodedata.normalize("NFC", str(text or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHAR_RE.sub("", normalized)
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def format_graph_node_embedding_input(name: str) -> str:
    cleaned = normalize_canonical_text_v1(name)
    if cleaned.startswith("entity_name: "):
        return cleaned
    return normalize_canonical_text_v1(f"entity_name: {cleaned}")


def format_graph_edge_embedding_input(fact: str) -> str:
    cleaned = normalize_canonical_text_v1(fact)
    if cleaned.startswith("relation_fact: "):
        return cleaned
    return normalize_canonical_text_v1(f"relation_fact: {cleaned}")


def compute_graph_node_input_hash(name: str) -> str:
    canonical = format_graph_node_embedding_input(name)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_graph_edge_input_hash(fact: str) -> str:
    canonical = format_graph_edge_embedding_input(fact)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


is_local_embedding_enabled = is_local_embedding_mode
compute_graph_node_embedding_input_hash = compute_graph_node_input_hash
compute_graph_edge_embedding_input_hash = compute_graph_edge_input_hash


def validate_local_query_vector(query_vector: list[float] | None) -> list[float]:
    if query_vector is None or len(query_vector) != LOCAL_GRAPH_VECTOR_DIM:
        actual = 0 if query_vector is None else len(query_vector)
        raise ValueError(
            f"Local graph profile {LOCAL_GRAPH_PROFILE_ID} requires {LOCAL_GRAPH_VECTOR_DIM}-dim "
            f"query vector, got {actual}"
        )
    return [float(x) for x in query_vector]


def strip_internal_graph_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    cleaned = dict(attributes)
    for key in INTERNAL_GRAPH_METADATA_PROPS:
        cleaned.pop(key, None)
    return cleaned


def prepare_entity_node_save_data(
    entity_data: dict[str, Any],
    *,
    local_mode: bool,
) -> dict[str, Any]:
    """Prepare EntityNode dictionary for Cypher persistence without cross-dimension overwrite."""
    prepared = dict(entity_data)
    for key in INTERNAL_GRAPH_METADATA_PROPS:
        if key not in {"local_name_embedding"}:
            prepared.pop(key, None)

    if not local_mode:
        prepared.pop("local_name_embedding", None)
        return prepared

    raw_vec = prepared.pop(NODE_LEGACY_EMBEDDING_PROP, None)
    if raw_vec is not None:
        if len(raw_vec) != LOCAL_GRAPH_VECTOR_DIM:
            raise ValueError(
                f"Local profile {LOCAL_GRAPH_PROFILE_ID} requires {LOCAL_GRAPH_VECTOR_DIM}-dim "
                f"entity name vector, got {len(raw_vec)}"
            )
        prepared["local_name_embedding"] = [float(x) for x in raw_vec]
        prepared[NODE_LOCAL_PROFILE_PROP] = LOCAL_GRAPH_PROFILE_ID
        prepared[NODE_LOCAL_INPUT_HASH_PROP] = compute_graph_node_input_hash(
            str(prepared.get("name") or "")
        )
        prepared[GRAPH_EMBEDDING_DIRTY_PROP] = False
    else:
        prepared["local_name_embedding"] = None
        prepared[NODE_LOCAL_INPUT_HASH_PROP] = compute_graph_node_input_hash(
            str(prepared.get("name") or "")
        )
        prepared[GRAPH_EMBEDDING_DIRTY_PROP] = True
    return prepared


def prepare_entity_edge_save_data(
    edge_data: dict[str, Any],
    *,
    local_mode: bool,
) -> dict[str, Any]:
    """Prepare EntityEdge dictionary for Cypher persistence without cross-dimension overwrite."""
    prepared = dict(edge_data)
    for key in INTERNAL_GRAPH_METADATA_PROPS:
        if key not in {"local_fact_embedding"}:
            prepared.pop(key, None)

    if not local_mode:
        prepared.pop("local_fact_embedding", None)
        return prepared

    raw_vec = prepared.pop(EDGE_LEGACY_EMBEDDING_PROP, None)
    if raw_vec is not None:
        if len(raw_vec) != LOCAL_GRAPH_VECTOR_DIM:
            raise ValueError(
                f"Local profile {LOCAL_GRAPH_PROFILE_ID} requires {LOCAL_GRAPH_VECTOR_DIM}-dim "
                f"edge fact vector, got {len(raw_vec)}"
            )
        prepared["local_fact_embedding"] = [float(x) for x in raw_vec]
        prepared[EDGE_LOCAL_PROFILE_PROP] = LOCAL_GRAPH_PROFILE_ID
        prepared[EDGE_LOCAL_INPUT_HASH_PROP] = compute_graph_edge_input_hash(
            str(prepared.get("fact") or "")
        )
        prepared[GRAPH_EMBEDDING_DIRTY_PROP] = False
    else:
        prepared["local_fact_embedding"] = None
        prepared[EDGE_LOCAL_INPUT_HASH_PROP] = compute_graph_edge_input_hash(
            str(prepared.get("fact") or "")
        )
        prepared[GRAPH_EMBEDDING_DIRTY_PROP] = True
    return prepared
