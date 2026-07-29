"""Backend-neutral codec for graph projection scopes and group IDs.

The derived graph is partitioned by an opaque, public-safe group ID. Episode
``brain_id`` is the authoritative scope when present; callers may provide a
default only for legacy episodes that do not carry one.
"""

from __future__ import annotations

import re

from ._util import public_safe_text, short_hash
from .models import OntologyEpisode


_GRAPH_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_GRAPH_SCOPE_CHARS = 200


def _public_safe_graph_scope(value: object) -> str:
    return public_safe_text(str(value or ""), max_chars=_MAX_GRAPH_SCOPE_CHARS)


def _authoritative_graph_scope(episode: OntologyEpisode, default_scope: str) -> str:
    return str(episode.payload.get("brain_id") or default_scope or "")


def graph_scope_for_episode(episode: OntologyEpisode, default_scope: str) -> str:
    """Return the episode brain scope, falling back only when it is absent."""

    brain_id = episode.payload.get("brain_id")
    if brain_id:
        return _public_safe_graph_scope(brain_id)
    return str(default_scope or "")


def graph_group_id(scope: str) -> str:
    """Encode an authoritative scope as a public-safe Graphiti group ID."""

    authoritative_scope = " ".join(str(scope or "").split())
    if not authoritative_scope:
        return ""
    public_scope = _public_safe_graph_scope(authoritative_scope)
    if public_scope == authoritative_scope and _GRAPH_GROUP_ID_RE.fullmatch(authoritative_scope):
        return authoritative_scope
    return f"brain_{short_hash(authoritative_scope)}"


def legacy_graph_group_id(scope: str) -> str:
    """Return the pre-full-scope Graphiti group ID for bounded read compatibility."""

    text = _public_safe_graph_scope(scope)
    if not text:
        return ""
    if _GRAPH_GROUP_ID_RE.fullmatch(text):
        return text
    return f"brain_{short_hash(text)}"


def graph_group_ids(scope: str) -> tuple[str, ...]:
    """Return the current ID plus at most one legacy ID for read-only lookup."""

    candidates = (graph_group_id(scope), legacy_graph_group_id(scope))
    return tuple(dict.fromkeys(group for group in candidates if group))


def graph_group_id_for_episode(episode: OntologyEpisode, default_scope: str) -> str:
    """Return the encoded group ID for an episode's authoritative graph scope."""

    return graph_group_id(_authoritative_graph_scope(episode, default_scope))


def episode_matches_graph_group(
    episode: OntologyEpisode,
    *,
    expected_group_id: str,
    default_scope: str,
) -> bool:
    """Require episode provenance to resolve to the requested graph group."""

    return graph_group_id_for_episode(episode, default_scope) == expected_group_id
