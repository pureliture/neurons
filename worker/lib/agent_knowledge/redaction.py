import re


PRIVATE_PATH_RE = re.compile(r"/Users/[^ \n\t'\"]+/\.[^/ \n\t'\"]+/private/[^ \n\t'\"]+")
PROVIDER_TRANSCRIPT_PATH_RE = re.compile(r"/Users/[^ \n\t'\"]+/\.(claude|gemini|codex)/[^ \n\t'\"]+", re.IGNORECASE)
AGENT_KNOWLEDGE_RUNTIME_PATH_RE = re.compile(r"/Users/[^ \n\t'\"]+/.openclaw/agent-knowledge/[^ \n\t'\"]+")
LOCAL_USER_PATH_RE = re.compile(r"/Users/[^\s]+", re.IGNORECASE)
LOCAL_HOME_PATH_RE = re.compile(r"~/[^\s]+", re.IGNORECASE)
LOCAL_PRIVATE_PATH_RE = re.compile(r"/private/[^\s]+", re.IGNORECASE)
LOCAL_VOLUMES_PATH_RE = re.compile(r"/Volumes/[^\s]+", re.IGNORECASE)
PRIVATE_LOCATOR_SENTINEL_RE = re.compile(
    r"(?:<private-[^>]*(?:path|locator|source)[^>]*>|\[private-[^\]]*(?:path|locator|source)[^\]]*\])",
    re.IGNORECASE,
)
PRIVATE_LOCATOR_TERM_RE = re.compile(r"\bprivate_locator\b", re.IGNORECASE)

DEFAULT_V1_PRIVATE_PATH_PATTERNS = (PRIVATE_PATH_RE,)
DEFAULT_PRIVATE_PATH_PATTERNS = (
    PRIVATE_PATH_RE,
    PROVIDER_TRANSCRIPT_PATH_RE,
    AGENT_KNOWLEDGE_RUNTIME_PATH_RE,
    PRIVATE_LOCATOR_SENTINEL_RE,
)
RAW_TRANSCRIPT_TERM_RE = re.compile(r"\braw[_-]?transcript[A-Za-z0-9_-]*\b", re.IGNORECASE)
DATASET_IDS_TERM_RE = re.compile(r"\bdataset_ids\b|\bdatasetId\b", re.IGNORECASE)
DATASET_ID_TERM_RE = re.compile(r"\bdataset_id\b", re.IGNORECASE)
DOCUMENT_IDS_TERM_RE = re.compile(r"\bdocument_ids\b|\bdocumentId\b", re.IGNORECASE)
DOCUMENT_ID_TERM_RE = re.compile(r"\bdocument_id\b", re.IGNORECASE)
AUTHORIZATION_TERM_RE = re.compile(r"\bAuthorization\b", re.IGNORECASE)
API_KEY_TERM_RE = re.compile(r"\bapi[_-]?key[A-Za-z0-9_-]*\b|\bapiKey[A-Za-z0-9_-]*\b", re.IGNORECASE)
ACCESS_TOKEN_TERM_RE = re.compile(r"\baccess[_-]?token[A-Za-z0-9_-]*\b|\btoken\b", re.IGNORECASE)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:export\s+)?[A-Z0-9_]*(TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^ \n\t,'\"}]+)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9][A-Za-z0-9._-]{8,}", re.IGNORECASE)
PUBLIC_BEARER_RE = re.compile(r"\bBearer\b\s*[^\s]*", re.IGNORECASE)
BASIC_AUTH_RE = re.compile(r"Basic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE)
COOKIE_HEADER_RE = re.compile(r"\b(Cookie|Set-Cookie)\b\s*[:=]\s*[^\n\r]+", re.IGNORECASE)
SERIALIZED_COOKIE_HEADER_RE = re.compile(
    r"([\"'](?:Cookie|Set-Cookie)[\"']\s*:\s*)([\"'])(.*?)(\2)",
    re.IGNORECASE,
)
AUTH_HEADER_RE = re.compile(
    r"\b(Authorization|X-Api-Key|Api-Key|Cookie|Set-Cookie)\b\s*[:=]\s*(Bearer\s+[^ \n\t,;]+|Basic\s+[^ \n\t,;]+|[^ \n\t,;]+)",
    re.IGNORECASE,
)
CREDENTIAL_URL_RE = re.compile(r"://[^/\s:@]+:[^@\s/]+@")
LOWER_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^ \n\t,'\"}]+)",
    re.IGNORECASE,
)


def redact_text(text: str, *, private_path_patterns=None) -> str:
    patterns = DEFAULT_V1_PRIVATE_PATH_PATTERNS if private_path_patterns is None else private_path_patterns
    redacted = text
    for pat in patterns:
        redacted = pat.sub("<redacted:private-path>", redacted)
    redacted = BEARER_RE.sub("Bearer <redacted:secret>", redacted)
    redacted = SECRET_ASSIGNMENT_RE.sub("<redacted:secret>", redacted)
    return redacted


def redact_text_v2(text: str, *, private_path_patterns=None) -> str:
    patterns = DEFAULT_PRIVATE_PATH_PATTERNS if private_path_patterns is None else private_path_patterns
    redacted = text
    for pat in patterns:
        redacted = pat.sub("<redacted:private-path>", redacted)
    redacted = CREDENTIAL_URL_RE.sub("://<redacted:secret>@", redacted)
    redacted = SERIALIZED_COOKIE_HEADER_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted:secret>{match.group(4)}", redacted)
    redacted = COOKIE_HEADER_RE.sub(lambda match: f"{match.group(1)}: <redacted:secret>", redacted)
    redacted = AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}: <redacted:secret>", redacted)
    redacted = BEARER_RE.sub("Bearer <redacted:secret>", redacted)
    redacted = BASIC_AUTH_RE.sub("Basic <redacted:secret>", redacted)
    redacted = SECRET_ASSIGNMENT_RE.sub("<redacted:secret>", redacted)
    redacted = LOWER_SECRET_ASSIGNMENT_RE.sub("<redacted:secret>", redacted)
    return redacted


def redact_public_ingress_text(text: str, *, private_path_patterns=None) -> str:
    """Apply the stricter public queue denylist used by rag-ingress-queue."""
    redacted = redact_text_v2(text, private_path_patterns=private_path_patterns)
    redacted = LOCAL_USER_PATH_RE.sub("[redacted_path]", redacted)
    redacted = LOCAL_HOME_PATH_RE.sub("[redacted_path]", redacted)
    redacted = LOCAL_PRIVATE_PATH_RE.sub("[redacted_path]", redacted)
    redacted = LOCAL_VOLUMES_PATH_RE.sub("[redacted_path]", redacted)
    redacted = AUTHORIZATION_TERM_RE.sub("credential_header", redacted)
    redacted = PUBLIC_BEARER_RE.sub("credential_scheme <redacted_secret>", redacted)
    redacted = API_KEY_TERM_RE.sub("credential_key", redacted)
    redacted = ACCESS_TOKEN_TERM_RE.sub("credential", redacted)
    redacted = DATASET_IDS_TERM_RE.sub("dataset_refs", redacted)
    redacted = DATASET_ID_TERM_RE.sub("dataset_ref", redacted)
    redacted = DOCUMENT_IDS_TERM_RE.sub("document_refs", redacted)
    redacted = DOCUMENT_ID_TERM_RE.sub("document_ref", redacted)
    redacted = PRIVATE_LOCATOR_TERM_RE.sub("redacted_locator", redacted)
    redacted = RAW_TRANSCRIPT_TERM_RE.sub("redacted transcript", redacted)
    return redacted
