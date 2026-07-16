from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


class TemporalSelectorError(ValueError):
    """Raised when a public temporal selector is malformed or contradictory."""


@dataclass(frozen=True)
class TemporalSelector:
    start: datetime
    end: datetime
    source: str

    def matches(self, *, observed_at_start: str, observed_at_end: str) -> bool:
        raw_start = str(observed_at_start or "").strip()
        raw_end = str(observed_at_end or "").strip()
        observed_start = parse_observed_at(raw_start)
        observed_end = parse_observed_at(raw_end)
        if (raw_start and observed_start is None) or (raw_end and observed_end is None):
            return False
        if observed_start is None and observed_end is None:
            return False
        observed_start = observed_start or observed_end
        observed_end = observed_end or observed_start
        assert observed_start is not None and observed_end is not None
        if observed_end < observed_start:
            return False
        return observed_start <= self.end and observed_end >= self.start

    def to_audit_dict(self) -> dict[str, str]:
        return {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "source": self.source,
        }


_ISO_TOKEN_RE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?)(?!\d)"
)


def parse_temporal_selector(
    *,
    as_of: str = "",
    date_from: str = "",
    date_to: str = "",
    query: str = "",
    now: datetime | None = None,
) -> TemporalSelector | None:
    as_of = str(as_of or "").strip()
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    if as_of and (date_from or date_to):
        raise TemporalSelectorError("as_of cannot be combined with date_from/date_to")
    if as_of:
        start, end = _selector_bounds(as_of, point_for_datetime=True)
        return TemporalSelector(start=start, end=end, source="as_of")
    if date_from or date_to:
        start = _selector_bounds(date_from, point_for_datetime=True)[0] if date_from else datetime.min.replace(tzinfo=timezone.utc)
        end = _selector_bounds(date_to, point_for_datetime=True)[1] if date_to else datetime.max.replace(tzinfo=timezone.utc)
        if start > end:
            raise TemporalSelectorError("date_from must not be after date_to")
        return TemporalSelector(start=start, end=end, source="date_range")

    text = str(query or "")
    token = _ISO_TOKEN_RE.search(text)
    if token:
        start, end = _selector_bounds(token.group(1), point_for_datetime=True)
        return TemporalSelector(start=start, end=end, source="query_iso_date")

    lowered = text.casefold()
    day_offset: int | None = None
    if "어제" in lowered or "yesterday" in lowered:
        day_offset = -1
    elif "오늘" in lowered or "today" in lowered:
        day_offset = 0
    if day_offset is None:
        return None
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected_day = reference.date() + timedelta(days=day_offset)
    start = datetime.combine(selected_day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(selected_day, time.max, tzinfo=timezone.utc)
    return TemporalSelector(start=start, end=end, source="query_relative_date")


def validate_explicit_temporal_selector(
    *,
    as_of: str = "",
    date_from: str = "",
    date_to: str = "",
    route: str = "",
) -> None:
    if as_of or date_from or date_to:
        if route and route != "temporal_work_recall":
            raise TemporalSelectorError(
                "explicit temporal selectors require route temporal_work_recall"
            )
        parse_temporal_selector(as_of=as_of, date_from=date_from, date_to=date_to)


def parse_observed_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _selector_bounds(value: str, *, point_for_datetime: bool) -> tuple[datetime, datetime]:
    text = str(value or "").strip()
    if not text:
        raise TemporalSelectorError("temporal selector must be non-empty")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            selected_day = date.fromisoformat(text)
        except ValueError as exc:
            raise TemporalSelectorError("temporal selector must be ISO-8601") from exc
        return (
            datetime.combine(selected_day, time.min, tzinfo=timezone.utc),
            datetime.combine(selected_day, time.max, tzinfo=timezone.utc),
        )
    parsed = parse_observed_at(text)
    if parsed is None:
        raise TemporalSelectorError("temporal selector must be ISO-8601")
    return (parsed, parsed) if point_for_datetime else (parsed, parsed)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "TemporalSelector",
    "TemporalSelectorError",
    "parse_observed_at",
    "parse_temporal_selector",
    "validate_explicit_temporal_selector",
]
