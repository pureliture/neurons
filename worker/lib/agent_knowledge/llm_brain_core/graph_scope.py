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


def graph_scope_for_episode(episode: OntologyEpisode, default_scope: str) -> str:
    """Return the episode brain scope, falling back only when it is absent."""

    brain_id = episode.payload.get("brain_id")
    if brain_id:
        return _public_safe_graph_scope(brain_id)
    return str(default_scope or "")


def graph_group_id(scope: str) -> str:
    """Encode a public-safe graph scope as a Graphiti-compatible group ID."""

    text = _public_safe_graph_scope(scope)
    if not text:
        return ""
    if _GRAPH_GROUP_ID_RE.fullmatch(text):
        return text
    return f"brain_{short_hash(text)}"


def graph_group_id_for_episode(episode: OntologyEpisode, default_scope: str) -> str:
    """Return the encoded group ID for an episode's authoritative graph scope."""

    return graph_group_id(graph_scope_for_episode(episode, default_scope))


def episode_matches_graph_group(
    episode: OntologyEpisode,
    *,
    expected_group_id: str,
    default_scope: str,
) -> bool:
    """Require episode provenance to resolve to the requested graph group."""

    return graph_group_id_for_episode(episode, default_scope) == expected_group_id
