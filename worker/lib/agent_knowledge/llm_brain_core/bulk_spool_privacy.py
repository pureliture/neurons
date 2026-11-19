"""Bounded privacy and canonicalization helpers for spooled source bodies."""

from __future__ import annotations
import hashlib
from typing import Iterable, Iterator
from agent_knowledge.couchdb_source.spool_reader import SpooledText
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid, privacy_gate
from agent_knowledge.llm_brain_core.runtime import _CHUNK_METADATA_HEADER_KEYS

_LINE_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_MAX_HEADER_CHARS = 64 * 1024
_MAX_PENDING_CHARS = 64 * 1024


class SlidingMarkerGate:
    """Fail-closed marker gate with overlap across arbitrary page boundaries."""

    def __init__(self, forbidden_markers: tuple[bytes, ...]) -> None:
        self._markers = tuple(forbidden_markers)
        self._overlap = max((len(marker) for marker in self._markers), default=0)
        if self._overlap:
            self._overlap -= 1
        self._carry = b""

    def feed(self, data: bytes) -> None:
        if not self._markers:
            return
        window = self._carry + data
        privacy_gate(window, forbidden_markers=self._markers, all_redacted=True)
        self._carry = window[-self._overlap:] if self._overlap else b""

    def finish(self) -> None:
        privacy_gate(b"", forbidden_markers=self._markers, all_redacted=True)


def _line_content(raw_line: str) -> str:
    parts = raw_line.splitlines()
    return parts[0] if parts else ""


def _has_line_break(raw_line: str) -> bool:
    return bool(raw_line) and raw_line[-1] in _LINE_BREAK_CHARS


def _split_complete_lines(data: str) -> tuple[list[str], str]:
    hold_cr = data.endswith("\r")
    work = data[:-1] if hold_cr else data
    parts = work.splitlines(keepends=True)
    if not parts:
        return [], "\r" if hold_cr else ""
    if _has_line_break(parts[-1]):
        complete, pending = parts, ""
    else:
        complete, pending = parts[:-1], parts[-1]
    if hold_cr:
        pending += "\r"
    return complete, pending


def detect_metadata_header(body: SpooledText) -> tuple[bool, int]:
    """Return (header_found, consumed_characters) from a bounded prefix."""
    buffer = ""
    consumed = 0
    metadata_lines = 0
    for page in body.iter_pages():
        if len(buffer) > _MAX_HEADER_CHARS:
            raise SourceInvalid("metadata header scan exceeds bounded window")
        buffer += page
        complete, buffer = _split_complete_lines(buffer)
        for raw_line in complete:
            consumed += len(raw_line)
            stripped = _line_content(raw_line).strip()
            if not stripped:
                if metadata_lines > 0:
                    return True, consumed
                return False, 0
            key, separator, _value = stripped.partition(":")
            if separator and key.strip() in _CHUNK_METADATA_HEADER_KEYS:
                metadata_lines += 1
                continue
            return False, 0
    if buffer:
        stripped = _line_content(buffer).strip()
        if not stripped and metadata_lines > 0:
            return True, consumed + len(buffer)
    return False, 0


def iter_normalized_pages(body: SpooledText, *, header_end: int) -> Iterator[str]:
    """Yield lines after a metadata header with newline-normalized joins."""
    pending = ""
    remaining = header_end
    for page in body.iter_pages():
        if remaining:
            if len(page) <= remaining:
                remaining -= len(page)
                continue
            page = page[remaining:]
            remaining = 0
        complete, pending = _split_complete_lines(pending + page)
        if len(pending) > _MAX_PENDING_CHARS:
            raise SourceInvalid("canonical line exceeds bounded scan window")
        for raw_line in complete:
            yield _line_content(raw_line) + "\n"
    if pending:
        yield _line_content(pending)


def iter_stripped_pages(pages: Iterable[str]) -> Iterator[str]:
    """Yield pages equivalent to ``text.strip()`` with bounded edge buffering."""
    pending_trailing = ""
    started = False
    for page in pages:
        if not started:
            page = page.lstrip()
            if not page:
                continue
            started = True
        combined = pending_trailing + page
        body = combined.rstrip()
        pending_trailing = combined[len(body):]
        if len(pending_trailing) > _MAX_PENDING_CHARS:
            raise SourceInvalid("canonical trailing whitespace exceeds bounded window")
        if body:
            yield body


class CanonicalSpooledText(SpooledText):
    """Canonical, bounded, gated page view of a spooled source document."""

    def __init__(
        self, source: SpooledText, *, lane: str, forbidden_markers: tuple[bytes, ...]
    ) -> None:
        self._source = source
        self.body: Any = getattr(source, "body", source)
        self.byte_length = source.byte_length
        self.content_hash = source.content_hash
        self._lane = lane
        self._markers = forbidden_markers
        self._plan: tuple[str, int] | None = None

    def _prepare(self) -> tuple[str, int]:
        if self._plan is None:
            if self._lane == "conversation":
                found, header_end = detect_metadata_header(self._source)
                self._plan = ("normalized", header_end) if found else ("raw", 0)
            else:
                self._plan = ("raw", 0)
        return self._plan

    def iter_pages(self, size: int = 64 * 1024) -> Iterator[str]:
        mode, header_end = self._prepare()
        source_pages: Iterable[str]
        if mode == "normalized":
            source_pages = iter_normalized_pages(self._source, header_end=header_end)
        else:
            source_pages = self._source.iter_pages()
        gate = SlidingMarkerGate(self._markers)
        for page in iter_stripped_pages(source_pages):
            data = page.encode("utf-8")
            gate.feed(data)
            yield page
        gate.finish()

    def close(self, *, retain: bool = False) -> None:
        self._source.close(retain=retain)


def measure_canonical_spool(
    body: SpooledText, *, lane: str, forbidden_markers: tuple[bytes, ...]
) -> tuple[CanonicalSpooledText, str, int]:
    """Validate all outbound pages before delivery; return view, digest, length."""
    canonical = CanonicalSpooledText(
        body, lane=lane, forbidden_markers=forbidden_markers
    )
    hasher = hashlib.sha256()
    total = 0
    try:
        for page in canonical.iter_pages():
            data = page.encode("utf-8")
            hasher.update(data)
            total += len(data)
    except Exception:
        canonical.close()
        raise
    return canonical, "sha256:" + hasher.hexdigest(), total
