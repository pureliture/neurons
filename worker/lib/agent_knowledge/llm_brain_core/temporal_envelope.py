"""Temporal envelope builder and Section 7.1 decision table engine.

Implements LBrain Information Age Milestone M4:
- Section 7.1 Decision Table (12 rows deterministic evaluation)
- Section 8 Response Contract (22 common temporal fields schema)
- Age bucket classification & clock fallback (FR-AGE-010 ~ 016)
- Freshness revalidation integration (FR-AGE-040 ~ 045)
- Public-safe one-line temporal summary (FR-AGE-095)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


# --- Constants & Thresholds (FR-AGE-014, FR-AGE-074) ---
AGE_BUCKET_FRESH_SECONDS = 86400  # <= 24h
AGE_BUCKET_RECENT_SECONDS = 7 * 86400  # <= 7d
AGE_BUCKET_AGING_SECONDS = 30 * 86400  # <= 30d
AGE_BUCKET_OLD_SECONDS = 180 * 86400  # <= 180d

CLOCK_SKEW_TOLERANCE_SECONDS = 300  # ±5 minutes tolerance (FR-AGE-074)

REQUIRED_22_TEMPORAL_FIELDS = {
    "observed_at",
    "recorded_at",
    "valid_from",
    "valid_to",
    "superseded_by",
    "as_of",
    "age",
    "ingestion_lag_seconds",
    "time_confidence",
    "currentness",
    "lifecycle_status",
    "freshness",
    "temporal_relation",
    "temporal_conflict",
    "projection_lag_seconds",
    "recommended_action",
    "summary",
}

REQUIRED_AGE_SUBFIELDS = {"seconds", "label", "clock", "confidence", "reason_codes"}
REQUIRED_FRESHNESS_SUBFIELDS = {"state", "checked_at", "next_check_after", "gaps"}


def _parse_instant(value: Any) -> tuple[datetime | None, bool]:
    """Parse a datetime or ISO-8601 string into a UTC timezone-aware datetime.

    Returns:
        (datetime_obj, timezone_missing_flag)
    """
    if value is None:
        return None, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc), True
        return value.astimezone(timezone.utc), False
    text = str(value).strip()
    if not text:
        return None, False
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc), True
        return dt.astimezone(timezone.utc), False
    except Exception:
        return None, False


def _to_iso(value: Any) -> str | None:
    """Format into ISO-8601 string or preserve string representation."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(value)


def evaluate_decision_table_7_1(
    *,
    age_label: str,
    currentness: str,
    valid_to_relation: str = "none",
    freshness_state: str = "unchecked",
    temporal_conflict: bool = False,
) -> tuple[str, str]:
    """Evaluate Section 7.1 Decision Table deterministically.

    Args:
        age_label: One of 'fresh', 'recent', 'aging', 'old', 'historical', 'unknown', 'future', 'young'
        currentness: One of 'current', 'superseded', 'stale', 'retired', 'conflicted', 'unknown'
        valid_to_relation: One of 'none', 'past', 'future', 'present'
        freshness_state: One of 'verified_current', 'verified_changed', 'unchecked', 'check_due', 'unverifiable', 'unknown'
        temporal_conflict: Boolean indicating cross-item or graph/pg conflict.

    Returns:
        (display_label, recommended_action)
    """
    # 1. Conflict takes top precedence (Row 12 / FR-AGE-028)
    if temporal_conflict or currentness == "conflicted":
        return ("현재 항목 간 충돌", "review_conflict")

    # 2. Time anomaly (Future timestamp) (Row 11 / FR-AGE-013, FR-AGE-071)
    if age_label == "future":
        return ("시간 이상", "ask_user")

    # 3. Unknown time / authority (Rows 9 & 10 / FR-AGE-003, FR-AGE-070)
    if currentness == "unknown" and age_label == "unknown":
        return ("시간/권위 불명", "ask_user")
    if age_label == "unknown" and currentness == "current":
        return ("현재 권위이나 나이 불명", "ask_user")
    if currentness == "unknown" or age_label == "unknown":
        return ("시간/권위 불명", "ask_user")

    # 4. Freshness source changed (Row 3 / FR-AGE-045)
    if freshness_state == "verified_changed":
        return ("현재 권위이나 소스 변경됨", "verify_or_supersede")

    # 5. Historical lanes (Superseded, Stale, Retired) (Rows 4, 5, 6, 7 / FR-AGE-021, 032, 033)
    if currentness == "superseded":
        if age_label in ("fresh", "recent", "young"):
            return ("최근이지만 이미 폐기됨", "prefer_replacement")
        return ("폐기됨·대체 있음", "prefer_replacement")

    if currentness == "stale":
        return ("신뢰 철회됨", "treat_as_historical")

    if currentness == "retired":
        return ("의도적으로 폐기됨", "prefer_replacement")

    if valid_to_relation == "past":
        return ("폐기됨·대체 있음", "prefer_replacement")

    # 6. Current lane (currentness == "current") (Rows 1, 2, 8)
    if freshness_state == "check_due":
        return ("오래됨·재검증 필요", "use_with_verification")

    if age_label in ("old", "historical"):
        if freshness_state == "verified_current":
            return ("오래되었지만 현재 유효", "use")
        return ("오래되었지만 현재 유효", "use_with_verification")

    if freshness_state == "verified_current":
        return ("최신·검증됨", "use")

    # Default for fresh/recent/aging with unchecked / unverifiable
    if age_label in ("fresh", "recent", "young"):
        return ("현재 유효", "use")

    return ("현재 유효", "use_with_verification")


def _generate_temporal_summary(
    obs_original_dt: datetime | None,
    clock: str,
    age_seconds: int | None,
    age_label: str,
    confidence: str,
    currentness: str,
    display_label: str,
) -> str:
    """Generate a concise, public-safe one-line temporal summary (FR-AGE-095)."""
    if obs_original_dt is None:
        if clock == "recorded_at" and age_seconds is not None:
            days = age_seconds // 86400
            return f"적재 {days}일 전"
        return "나이 불명"

    if confidence == "anomalous" or age_label == "future":
        return "시간 이상"

    if age_seconds is None:
        return "나이 불명"

    days = age_seconds // 86400
    if days == 0:
        hours = age_seconds // 3600
        return f"관측 {hours}시간 전" if hours > 0 else "방금 관측"
    return f"관측 {days}일 전"


def build_temporal_envelope(
    *,
    observed_at: datetime | str | None,
    recorded_at: datetime | str | None,
    valid_from: datetime | str | None = None,
    valid_to: datetime | str | None = None,
    superseded_by: list[str] | str | None = None,
    as_of: datetime | str | None = None,
    currentness: str = "current",
    lifecycle_status: str = "accepted",
    freshness_state: str | None = None,
    freshness_checked_at: datetime | str | None = None,
    freshness_next_check_after: datetime | str | None = None,
    freshness_gaps: list[str] | None = None,
    source_hash: str | None = None,
    expected_source_hash: str | None = None,
    temporal_conflict: bool = False,
    projection_lag_seconds: float | int | None = None,
    authority_source: str | None = None,
    card_type: str | None = None,
    task_state: str | None = None,
    time_confidence: str | None = None,
) -> dict[str, Any]:
    """Construct a Section 8 compliant 22-field temporal envelope.

    Args:
        observed_at: Source observation timestamp.
        recorded_at: Ingestion/creation timestamp.
        valid_from: Beginning of validity period.
        valid_to: End of validity period.
        superseded_by: Identifier(s) of superseding item(s).
        as_of: Query evaluation instant (defaults to UTC now).
        currentness: Currentness status ('current', 'superseded', etc.).
        lifecycle_status: Lifecycle status (default 'accepted').
        freshness_state: Raw freshness state.
        freshness_checked_at: Freshness verification timestamp.
        freshness_next_check_after: Freshness recheck deadline.
        freshness_gaps: List of freshness gap codes.
        source_hash: Observed source content hash.
        expected_source_hash: Expected/latest source content hash.
        temporal_conflict: Cross-source or cross-lane conflict indicator.
        projection_lag_seconds: Graph projection lag in seconds.
        authority_source: Authority source name (e.g. 'postgres').
        card_type: Card classification (e.g. 'decision', 'task').
        task_state: Task execution state (e.g. 'in_progress').
        time_confidence: Optional override for time confidence.

    Returns:
        Dictionary containing all 17 top-level temporal fields, nested age (5 subfields),
        and nested freshness (4 subfields).
    """
    # 1. Parse timestamps
    obs_original_dt, obs_tz_missing = _parse_instant(observed_at)
    rec_dt, rec_tz_missing = _parse_instant(recorded_at)
    v_from_dt, _ = _parse_instant(valid_from)
    v_to_dt, _ = _parse_instant(valid_to)
    as_of_dt, _ = _parse_instant(as_of)
    if as_of_dt is None:
        as_of_dt = datetime.now(timezone.utc)

    # 2. Clock fallback (FR-AGE-011, FR-AGE-012)
    clock = "observed_at"
    effective_obs_dt = obs_original_dt
    if effective_obs_dt is None and rec_dt is not None:
        effective_obs_dt = rec_dt
        clock = "recorded_at"

    # 3. Calculate Age and detect anomalies (FR-AGE-010, 013, 070, 071, 072, 073, 074)
    reason_codes: list[str] = []
    age_seconds: int | None = None
    age_label = "unknown"
    confidence = "unknown"

    if effective_obs_dt is None:
        reason_codes.append("observed_at_missing")
        clock = "unknown"
        age_label = "unknown"
        confidence = "unknown"
    else:
        delta = (as_of_dt - effective_obs_dt).total_seconds()
        if delta < -CLOCK_SKEW_TOLERANCE_SECONDS:
            # Future timestamp beyond tolerance (FR-AGE-013, FR-AGE-071)
            confidence = "anomalous"
            age_label = "future"
            age_seconds = int(delta)
            reason_codes.append("observed_after_now")
        else:
            age_seconds = max(0, int(delta))
            confidence = "low" if clock == "recorded_at" else "high"
            if clock == "recorded_at":
                reason_codes.append("observed_at_missing")
            if obs_tz_missing or (clock == "recorded_at" and rec_tz_missing):
                reason_codes.append("timezone_missing")
                if confidence == "high":
                    confidence = "medium"

            # Bucket classification (FR-AGE-014)
            days = age_seconds // 86400
            if days < 1:
                age_label = "fresh"
            elif days < 7:
                age_label = "recent"
            elif days < 30:
                age_label = "aging"
            elif days < 180:
                age_label = "old"
            else:
                age_label = "historical"

    # Detect observed > recorded anomaly (FR-AGE-071)
    if obs_original_dt is not None and rec_dt is not None:
        if (obs_original_dt - rec_dt).total_seconds() > CLOCK_SKEW_TOLERANCE_SECONDS:
            confidence = "anomalous"
            if "observed_after_recorded" not in reason_codes:
                reason_codes.append("observed_after_recorded")

    # Detect valid_from > valid_to anomaly (FR-AGE-072)
    if v_from_dt is not None and v_to_dt is not None:
        if v_from_dt > v_to_dt:
            confidence = "anomalous"
            if "invalid_validity_window" not in reason_codes:
                reason_codes.append("invalid_validity_window")

    if time_confidence is not None:
        confidence = time_confidence

    # Ingestion lag (FR-AGE-001, AC-AGE-001)
    ingestion_lag_seconds: int | None = None
    if obs_original_dt is not None and rec_dt is not None:
        try:
            ingestion_lag_seconds = int((rec_dt - obs_original_dt).total_seconds())
        except Exception:
            ingestion_lag_seconds = None

    # Temporal relation (Section 8 YAML)
    if confidence == "anomalous" or age_label == "future":
        temporal_relation = "future"
    elif v_to_dt is not None and v_to_dt < as_of_dt:
        temporal_relation = "past"
    elif v_from_dt is not None and v_from_dt > as_of_dt:
        temporal_relation = "future"
    elif age_seconds is None:
        temporal_relation = "unknown"
    elif age_seconds > 0:
        temporal_relation = "past"
    else:
        temporal_relation = "present"

    # 4. Freshness Revalidation Logic (FR-AGE-040 ~ 045, AC-AGE-010, AC-AGE-012)
    gaps: list[str] = list(freshness_gaps or [])
    checked_at_dt, _ = _parse_instant(freshness_checked_at)
    next_check_dt, _ = _parse_instant(freshness_next_check_after)

    # Rule 1: Missing checked_at -> state='unchecked', gap='no_recent_freshness_check'
    if checked_at_dt is None:
        derived_freshness_state = "unchecked"
        if "no_recent_freshness_check" not in gaps:
            gaps.append("no_recent_freshness_check")
    else:
        derived_freshness_state = str(freshness_state or "unchecked")

    # Rule 3: source_hash mismatch / verified_changed (FR-AGE-042/045)
    source_changed = False
    if expected_source_hash is not None and source_hash is not None:
        if str(expected_source_hash).strip() != str(source_hash).strip():
            source_changed = True
            if "source_changed" not in gaps:
                gaps.append("source_changed")
    elif freshness_state == "verified_changed" or "source_changed" in gaps or "source_hash_mismatch" in gaps:
        source_changed = True

    if source_changed:
        derived_freshness_state = "verified_changed"

    # Rule 2: next_check_after expired -> state='check_due' (FR-AGE-044)
    is_check_due = False
    if next_check_dt is not None and as_of_dt >= next_check_dt:
        is_check_due = True

    if derived_freshness_state != "verified_changed" and is_check_due:
        derived_freshness_state = "check_due"

    # Task card protection (FR-AGE-035, AC-AGE-012)
    is_task = (str(card_type or "") in ("task", "work", "work_unit")) or (task_state is not None)
    if is_task:
        task_age_60d = (age_seconds is not None and age_seconds >= 60 * 86400)
        if task_age_60d or is_check_due:
            if derived_freshness_state != "verified_changed":
                derived_freshness_state = "check_due"

    # 5. Validity window relation (Section 7.1)
    if v_to_dt is None:
        valid_to_relation = "none"
    elif v_to_dt < as_of_dt:
        valid_to_relation = "past"
    elif v_to_dt > as_of_dt:
        valid_to_relation = "future"
    else:
        valid_to_relation = "present"

    effective_conflict = bool(temporal_conflict) or currentness == "conflicted"

    # 6. Evaluate Decision Table 7.1
    display_label, recommended_action = evaluate_decision_table_7_1(
        age_label=age_label,
        currentness=currentness,
        valid_to_relation=valid_to_relation,
        freshness_state=derived_freshness_state,
        temporal_conflict=effective_conflict,
    )

    # 7. Generate public-safe summary (FR-AGE-095)
    summary = _generate_temporal_summary(
        obs_original_dt=obs_original_dt,
        clock=clock,
        age_seconds=age_seconds,
        age_label=age_label,
        confidence=confidence,
        currentness=currentness,
        display_label=display_label,
    )

    # 8. Assemble subblocks and top-level fields (Section 8 22 fields)
    sup_list = (
        superseded_by
        if isinstance(superseded_by, list)
        else [superseded_by]
        if superseded_by is not None
        else []
    )

    proj_lag: int | float | None = None
    if projection_lag_seconds is not None:
        try:
            f_val = float(projection_lag_seconds)
            proj_lag = int(f_val) if f_val.is_integer() else f_val
        except (ValueError, TypeError):
            proj_lag = None

    age_subblock: dict[str, Any] = {
        "seconds": age_seconds,
        "label": age_label,
        "clock": clock if effective_obs_dt is not None else "unknown",
        "confidence": confidence,
        "reason_codes": reason_codes,
    }

    freshness_subblock: dict[str, Any] = {
        "state": derived_freshness_state,
        "checked_at": _to_iso(checked_at_dt),
        "next_check_after": _to_iso(next_check_dt),
        "gaps": gaps,
    }

    envelope: dict[str, Any] = {
        "observed_at": _to_iso(observed_at) if obs_original_dt is not None else None,
        "recorded_at": _to_iso(recorded_at) if rec_dt is not None else None,
        "valid_from": _to_iso(valid_from) if v_from_dt is not None else None,
        "valid_to": _to_iso(valid_to) if v_to_dt is not None else None,
        "superseded_by": sup_list,
        "as_of": _to_iso(as_of) if as_of else as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age": age_subblock,
        "ingestion_lag_seconds": ingestion_lag_seconds,
        "time_confidence": confidence,
        "currentness": currentness,
        "lifecycle_status": lifecycle_status or "accepted",
        "freshness": freshness_subblock,
        "temporal_relation": temporal_relation,
        "temporal_conflict": effective_conflict,
        "projection_lag_seconds": proj_lag,
        "recommended_action": recommended_action,
        "summary": summary,
    }

    if authority_source is not None or effective_conflict:
        envelope["authority_source"] = authority_source or "postgres"

    return envelope


def build_card_temporal_envelope(
    card: Mapping[str, Any],
    as_of: datetime | str | None = None,
    projection_lag_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Helper to build a temporal envelope directly from a memory card mapping."""
    raw_typed = card.get("typed_payload")
    typed: Mapping[str, Any] = raw_typed if isinstance(raw_typed, Mapping) else {}

    proj_lag = projection_lag_seconds
    if proj_lag is None:
        if card.get("projection_lag_seconds") is not None:
            proj_lag = card["projection_lag_seconds"]
        elif card.get("projection_lag_ms") is not None:
            try:
                proj_lag = float(card["projection_lag_ms"]) / 1000.0
            except (ValueError, TypeError):
                proj_lag = None

    currentness = str(card.get("currentness") or "current")
    temporal_conflict = bool(card.get("temporal_conflict")) or currentness == "conflicted"
    authority_source = card.get("authority_source") or ("postgres" if temporal_conflict else None)

    return build_temporal_envelope(
        observed_at=card.get("observed_at") or typed.get("observed_at"),
        recorded_at=card.get("recorded_at") or card.get("created_at") or card.get("updated_at"),
        valid_from=card.get("valid_from"),
        valid_to=card.get("valid_to"),
        superseded_by=card.get("superseded_by"),
        as_of=as_of or None,
        currentness=currentness,
        lifecycle_status=str(card.get("lifecycle_status") or card.get("lifecycle_state") or "accepted"),
        freshness_state=card.get("freshness_state") or typed.get("freshness_state"),
        freshness_checked_at=card.get("freshness_checked_at") or typed.get("freshness_checked_at"),
        freshness_next_check_after=card.get("freshness_next_check_after") or typed.get("freshness_next_check_after"),
        freshness_gaps=card.get("freshness_gaps") or typed.get("freshness_gaps"),
        source_hash=card.get("source_hash") or typed.get("source_hash"),
        expected_source_hash=card.get("expected_source_hash") or card.get("latest_source_hash") or typed.get("expected_source_hash"),
        temporal_conflict=temporal_conflict,
        projection_lag_seconds=proj_lag,
        authority_source=str(authority_source) if authority_source else None,
        card_type=str(card.get("card_type") or ""),
        task_state=str(typed.get("task_state") or "") if typed.get("task_state") else None,
        time_confidence=card.get("time_confidence"),
    )
