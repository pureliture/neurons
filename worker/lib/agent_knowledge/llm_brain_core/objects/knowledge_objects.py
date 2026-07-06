from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text, require_sha256, short_hash, utc_now_iso


LIFECYCLE_STATUSES = {
    "observed",
    "extracted",
    "proposed",
    "accepted",
    "current",
    "stale",
    "superseded",
    "retired",
    "rejected",
    "archived",
}
AUTHORITY_LANES = {
    "reference_only",
    "candidate",
    "proposal_only",
    "accepted_current",
    "accepted_non_current",
    "derived_projection",
    "archive_only",
    "rejected",
}
VERIFICATION_STATES = {
    "not_applicable",
    "unverified",
    "source_hash_verified",
    "freshness_checked",
    "test_verified",
    "runtime_verified",
    "runtime_unverified",
}
REVIEW_STATES = {"not_required", "needs_review", "accepted", "rejected"}
PRIVACY_CLASSES = {"public_safe", "local_private", "private_sensitive", "secret_forbidden"}

ACCEPTED_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
ACCEPTED_APPROVAL_STATES = {"approved", "auto_accepted"}
NON_CURRENT_CARD_STATES = {"stale", "superseded", "archive_candidate"}


def _require_enum(value: str, allowed: set[str], field_name: str) -> str:
    text = str(value or "")
    if text not in allowed:
        raise ValueError(f"{field_name} must be one of {sorted(allowed)}")
    return text


def validate_state_axes(
    *,
    lifecycle_status: str,
    authority_lane: str,
    verification_state: str,
    review_state: str,
) -> None:
    _require_enum(lifecycle_status, LIFECYCLE_STATUSES, "lifecycle_status")
    _require_enum(authority_lane, AUTHORITY_LANES, "authority_lane")
    _require_enum(verification_state, VERIFICATION_STATES, "verification_state")
    _require_enum(review_state, REVIEW_STATES, "review_state")
    if verification_state == "runtime_verified" and authority_lane in {
        "reference_only",
        "proposal_only",
        "archive_only",
        "derived_projection",
    }:
        raise ValueError("runtime_verified requires accepted or candidate runtime authority lane")
    if authority_lane == "accepted_current" and review_state != "accepted":
        raise ValueError("accepted_current requires review_state=accepted")
    if authority_lane in {"candidate", "proposal_only"} and review_state == "accepted":
        raise ValueError("candidate/proposal_only cannot be accepted")


def _locator_display_ref(locator_kind: str, value: str) -> tuple[str, bool]:
    if locator_kind in {"relative_repo_path", "repo_path"} and value and not value.startswith(("/", "~")):
        return public_safe_text(value, max_chars=240), False
    if locator_kind in {"public_url", "docs_url"} and value:
        return public_safe_text(value, max_chars=240), False
    safe_kind = locator_kind or "opaque"
    ensure_public_safe(safe_kind, "locator_kind")
    return f"{safe_kind}:redacted", True


def _object_id(object_type: str, natural_key: str, scope: Mapping[str, Any], content_hash: str) -> str:
    return f"ko:{object_type}:{short_hash([object_type, natural_key, dict(scope), content_hash])}"


@dataclass(frozen=True)
class KnowledgeObjectEnvelope:
    object_id: str
    object_type: str
    scope: dict[str, Any]
    title: str
    summary: str
    lifecycle_status: str
    authority_lane: str
    verification_state: str
    review_state: str
    content_hash: str
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    edge_refs: tuple[str, ...] = ()
    observed_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    confidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    freshness: dict[str, Any] = field(default_factory=dict)
    privacy_class: str = "public_safe"
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_state_axes(
            lifecycle_status=self.lifecycle_status,
            authority_lane=self.authority_lane,
            verification_state=self.verification_state,
            review_state=self.review_state,
        )
        require_sha256(self.content_hash, "content_hash")
        _require_enum(self.privacy_class, PRIVACY_CLASSES, "privacy_class")
        object.__setattr__(self, "title", public_safe_text(self.title, max_chars=240))
        object.__setattr__(self, "summary", public_safe_text(self.summary, max_chars=1024))
        ensure_public_safe(self.to_dict(), "KnowledgeObjectEnvelope")

    @classmethod
    def from_parts(
        cls,
        *,
        object_type: str,
        natural_key: str,
        scope: Mapping[str, Any],
        title: str,
        summary: str,
        lifecycle_status: str,
        authority_lane: str,
        verification_state: str,
        review_state: str,
        content_hash: str,
        source_refs: list[str] | tuple[str, ...] = (),
        evidence_refs: list[str] | tuple[str, ...] = (),
        edge_refs: list[str] | tuple[str, ...] = (),
        observed_at: str = "",
        confidence: Mapping[str, Any] | None = None,
        recommended_action: str = "",
        freshness: Mapping[str, Any] | None = None,
        privacy_class: str = "public_safe",
        payload: Mapping[str, Any] | None = None,
    ) -> "KnowledgeObjectEnvelope":
        return cls(
            object_id=_object_id(object_type, natural_key, scope, content_hash),
            object_type=object_type,
            scope=dict(scope),
            title=title,
            summary=summary,
            lifecycle_status=lifecycle_status,
            authority_lane=authority_lane,
            verification_state=verification_state,
            review_state=review_state,
            content_hash=content_hash,
            source_refs=tuple(str(item) for item in source_refs),
            evidence_refs=tuple(str(item) for item in evidence_refs),
            edge_refs=tuple(str(item) for item in edge_refs),
            observed_at=observed_at or utc_now_iso(),
            confidence=dict(confidence or {}),
            recommended_action=public_safe_text(recommended_action, max_chars=120),
            freshness=dict(freshness or {}),
            privacy_class=privacy_class,
            payload=dict(payload or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_refs"] = list(self.source_refs)
        data["evidence_refs"] = list(self.evidence_refs)
        data["edge_refs"] = list(self.edge_refs)
        data["schema_version"] = "knowledge_object_envelope.v1"
        return data


@dataclass(frozen=True)
class KnowledgeEdge:
    edge_id: str
    edge_type: str
    from_object_id: str
    to_object_id: str
    evidence_refs: tuple[str, ...]
    lifecycle_status: str
    authority_lane: str
    verification_state: str
    direction: str = "forward"
    confidence: dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""
    freshness: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_state_axes(
            lifecycle_status=self.lifecycle_status,
            authority_lane=self.authority_lane,
            verification_state=self.verification_state,
            review_state="needs_review" if self.authority_lane == "proposal_only" else "not_required",
        )
        ensure_public_safe(self.to_dict(), "KnowledgeEdge")

    @classmethod
    def from_parts(
        cls,
        *,
        edge_type: str,
        from_object_id: str,
        to_object_id: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
        lifecycle_status: str,
        authority_lane: str,
        verification_state: str,
        confidence: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "KnowledgeEdge":
        edge_id = f"ke:{edge_type}:{short_hash([edge_type, from_object_id, to_object_id, list(evidence_refs)])}"
        return cls(
            edge_id=edge_id,
            edge_type=edge_type,
            from_object_id=from_object_id,
            to_object_id=to_object_id,
            evidence_refs=tuple(str(item) for item in evidence_refs),
            lifecycle_status=lifecycle_status,
            authority_lane=authority_lane,
            verification_state=verification_state,
            confidence=dict(confidence or {}),
            observed_at=utc_now_iso(),
            payload=dict(payload or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["schema_version"] = "knowledge_edge.v1"
        return data


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    evidence_type: str
    authority_lane: str
    verification_state: str
    locator: dict[str, Any]
    content_hash: str
    observed_at: str
    summary: str
    producer: dict[str, Any] = field(default_factory=dict)
    privacy_class: str = "public_safe"
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum(self.authority_lane, AUTHORITY_LANES, "authority_lane")
        _require_enum(self.verification_state, VERIFICATION_STATES, "verification_state")
        require_sha256(self.content_hash, "content_hash")
        ensure_public_safe(self.to_dict(), "EvidenceRef")

    @classmethod
    def from_parts(
        cls,
        *,
        evidence_type: str,
        authority_lane: str,
        verification_state: str,
        locator: Mapping[str, Any],
        content_hash: str,
        summary: str,
        producer: Mapping[str, Any] | None = None,
        privacy_class: str = "public_safe",
        gaps: list[str] | tuple[str, ...] = (),
    ) -> "EvidenceRef":
        evidence_id = f"ev:{evidence_type}:{short_hash([evidence_type, dict(locator), content_hash])}"
        return cls(
            evidence_id=evidence_id,
            evidence_type=evidence_type,
            authority_lane=authority_lane,
            verification_state=verification_state,
            locator=dict(locator),
            content_hash=content_hash,
            observed_at=utc_now_iso(),
            summary=public_safe_text(summary, max_chars=512),
            producer=dict(producer or {}),
            privacy_class=privacy_class,
            gaps=tuple(str(item) for item in gaps),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gaps"] = list(self.gaps)
        data["schema_version"] = "evidence_ref.v1"
        return data

    def to_view(self) -> dict[str, Any]:
        value = str(self.locator.get("value") or self.locator.get("display_ref") or "")
        locator_kind = str(self.locator.get("kind") or "opaque")
        display_ref, display_ref_redacted = _locator_display_ref(locator_kind, value)
        view = {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "authority_lane": self.authority_lane,
            "verification_state": self.verification_state,
            "locator_view": {
                "locator_kind": locator_kind,
                "display_ref": display_ref,
                "display_ref_redacted": display_ref_redacted,
                "locator_digest": hash_payload({"kind": locator_kind, "value": value}),
            },
            "summary": self.summary,
            "raw_return_capability": "denied",
        }
        ensure_public_safe(view, "EvidenceRefView")
        return view


@dataclass(frozen=True)
class ReviewProposal:
    proposal_id: str
    proposal_type: str
    target_object_id: str
    reason: str
    evidence_refs: tuple[str, ...]
    proposer: str
    status: str = "needs_review"
    created_at: str = ""

    @classmethod
    def from_parts(
        cls,
        *,
        proposal_type: str,
        target_object_id: str,
        reason: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
        proposer: str = "unspecified",
    ) -> "ReviewProposal":
        proposal_id = f"proposal:{short_hash([proposal_type, target_object_id, reason, list(evidence_refs)])}"
        return cls(
            proposal_id=proposal_id,
            proposal_type=proposal_type,
            target_object_id=target_object_id,
            reason=public_safe_text(reason, max_chars=512),
            evidence_refs=tuple(str(item) for item in evidence_refs),
            proposer=proposer,
            created_at=utc_now_iso(),
        )

    def to_dict(self, *, proposal_write_performed: bool = False, proposal_write_target: str = "") -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["schema_version"] = "review_proposal.v1"
        data["proposal_preview_created"] = True
        data["proposal_write_performed"] = bool(proposal_write_performed)
        data["proposal_write_target"] = public_safe_text(proposal_write_target, max_chars=120)
        data["authority_write_performed"] = False
        data["authoritative_memory_changed"] = False
        ensure_public_safe(data, "ReviewProposal")
        return data


@dataclass(frozen=True)
class AuthorityDecision:
    decision_id: str
    decision_type: str
    target_object_id: str
    previous_authority_lane: str
    new_authority_lane: str
    approved_by: str
    evidence_refs: tuple[str, ...]
    approved_at: str

    @classmethod
    def from_parts(
        cls,
        *,
        decision_type: str,
        target_object_id: str,
        previous_authority_lane: str,
        new_authority_lane: str,
        approved_by: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> "AuthorityDecision":
        _require_enum(previous_authority_lane, AUTHORITY_LANES, "previous_authority_lane")
        _require_enum(new_authority_lane, AUTHORITY_LANES, "new_authority_lane")
        decision_id = f"decision:{short_hash([decision_type, target_object_id, previous_authority_lane, new_authority_lane])}"
        return cls(
            decision_id=decision_id,
            decision_type=decision_type,
            target_object_id=target_object_id,
            previous_authority_lane=previous_authority_lane,
            new_authority_lane=new_authority_lane,
            approved_by=public_safe_text(approved_by, max_chars=120),
            evidence_refs=tuple(str(item) for item in evidence_refs),
            approved_at=utc_now_iso(),
        )

    def to_dict(self, *, authority_write_performed: bool = False, cache_invalidated: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["schema_version"] = "authority_decision.v1"
        data["authority_decision_preview_created"] = True
        data["proposal_write_performed"] = False
        data["authority_write_performed"] = bool(authority_write_performed)
        data["authoritative_memory_changed"] = bool(authority_write_performed)
        data["cache_invalidated"] = bool(cache_invalidated)
        ensure_public_safe(data, "AuthorityDecision")
        return data


def memory_card_to_knowledge_object(card: Mapping[str, Any]) -> KnowledgeObjectEnvelope:
    lifecycle = str(card.get("lifecycle_state") or "")
    approval = str(card.get("approval_state") or "")
    currentness = str(card.get("currentness") or "current")
    if lifecycle in ACCEPTED_LIFECYCLE_STATES and approval in ACCEPTED_APPROVAL_STATES:
        if currentness in NON_CURRENT_CARD_STATES:
            lifecycle_status = "stale" if currentness == "archive_candidate" else currentness
            authority_lane = "accepted_non_current"
        else:
            lifecycle_status = "current"
            authority_lane = "accepted_current"
        review_state = "accepted"
    elif lifecycle in {"candidate", "needs_review"}:
        lifecycle_status = "proposed"
        authority_lane = "candidate"
        review_state = "needs_review"
    else:
        lifecycle_status = "rejected" if lifecycle == "rejected" else "observed"
        authority_lane = "rejected" if lifecycle == "rejected" else "candidate"
        review_state = "rejected" if lifecycle == "rejected" else "needs_review"
    content_hash = str(card.get("content_hash") or hash_payload({"memory_id": card.get("memory_id"), "summary": card.get("summary")}))
    return KnowledgeObjectEnvelope.from_parts(
        object_type=f"MemoryCard:{card.get('card_type') or 'unknown'}",
        natural_key=str(card.get("memory_id") or card.get("title") or content_hash),
        scope={"project": str(card.get("project") or ""), "provider": str(card.get("provider") or "")},
        title=str(card.get("title") or card.get("memory_id") or "MemoryCard"),
        summary=str(card.get("summary") or ""),
        lifecycle_status=lifecycle_status,
        authority_lane=authority_lane,
        verification_state="source_hash_verified",
        review_state=review_state,
        content_hash=content_hash,
        evidence_refs=[str(card.get("memory_id") or "")] if card.get("memory_id") else [],
        confidence={"score": float(card.get("confidence") or 0), "basis": str(card.get("confidence_basis") or "")},
        payload={"memory_id": str(card.get("memory_id") or ""), "card_type": str(card.get("card_type") or "")},
    )


def denied_payload(tool_name: str, reason: str, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "schema_version": "object_substrate_denied.v1",
        "tool": tool_name,
        "permission": "denied",
        "reason": reason,
        "proposal_write_performed": False,
        "authority_write_performed": False,
        "authoritative_memory_changed": False,
    }
    if extra:
        payload.update(dict(extra))
    ensure_public_safe(payload, "ObjectSubstrateDeniedPayload")
    return payload
