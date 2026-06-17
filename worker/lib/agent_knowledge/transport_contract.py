from dataclasses import dataclass

@dataclass(frozen=True)
class ProxyResponse:
    status_code: int
    body: bytes
    headers: dict[str, str] | None = None
