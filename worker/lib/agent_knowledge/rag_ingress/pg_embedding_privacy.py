"""Secret egress preflight for private-tailnet PG embedding, not public output.

Internal locators and technical vocabulary are not secrets. Reject concrete
credential-shaped values without rewriting text or changing its hash. These
heuristics are defense in depth, not a complete secret detector: arbitrary
unlabelled passwords, novel formats and obfuscated secrets cannot be identified
reliably. Callers must still keep credentials out of embedding inputs.
"""
from __future__ import annotations

import base64
import binascii
import re


_ERROR = "PG embedding input rejected by secret egress policy"
# Allow only whole, explicit redaction markers (not prefixes with secret tails).
_REDACTED = re.compile(
    r"(?:<redacted(?:[:_-]secret)?>|\[redacted(?:[:_-]secret)?\]|\*{3,})",
    re.IGNORECASE,
)
_VALUE = r'''(?:"(?P<double>(?:\\.|[^"\\])*)"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<bare>[^\s,;\}"']+))'''
_ASSIGNMENT = re.compile(
    r'''(?<![\w-])["']?[\w-]*(?:password|passwd|secret|token|api[_-]?key)["']?\s*[:=]\s*'''
    + _VALUE,
    re.IGNORECASE,
)

_AUTHORIZATION = re.compile(
    r'''\bAuthorization["']?\s*[:=]\s*'''
    + r'''(?:"(?P<double>(?:\\.|[^"\\])*)"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<bare>[^\r\n,;}]+))''',
    re.IGNORECASE,
)
_SCHEME = re.compile(r"^(?:Bearer|Basic)\s+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer[ \t]+([A-Za-z0-9._~+/-]+=*)", re.IGNORECASE)
_BASIC = re.compile(r"\bBasic[ \t]+([A-Za-z0-9+/]+=*)", re.IGNORECASE)
_CREDENTIAL_URL = re.compile(r"://[^/\s:@]+:([^@\s/]+)@")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----\s*"
    r"(.*?)(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


def _has_value(value: str) -> bool:
    return bool(value.strip()) and _REDACTED.fullmatch(value.strip()) is None


def assert_pg_embedding_egress_safe(text: str) -> None:
    """Raise a fixed, source-free error before external egress; never mutate text."""
    if _KNOWN_TOKEN.search(text):
        raise ValueError(_ERROR)
    for pattern in (_ASSIGNMENT, _AUTHORIZATION):
        for match in pattern.finditer(text):
            value = next(value for value in match.groups() if value is not None)
            if pattern is _AUTHORIZATION:
                value = _SCHEME.sub("", value.strip())
            if _has_value(value):
                raise ValueError(_ERROR)
    for pattern in (_CREDENTIAL_URL, _PRIVATE_KEY):
        for match in pattern.finditer(text):
            if _has_value(match.group(1)):
                raise ValueError(_ERROR)
    # Outside a header/assignment, require a token shape rather than a word
    # such as "Bearer authentication". Header values above are conservative.
    for match in _BEARER.finditer(text):
        value = match.group(1)
        if len(value) >= 8 and re.search(r"[0-9._~+/-]", value):
            raise ValueError(_ERROR)
    for match in _BASIC.finditer(text):
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
        except (ValueError, binascii.Error):
            continue
        if b":" in decoded:
            raise ValueError(_ERROR)
