# MCP HTTP Allowed Hosts Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 승인 상태: 사용자가 `requirements.md`와 후속 `design.md`를 사전 승인함.

## 질문-답변 흐름

### Q: 장기 보증 상태에서 수정 주 레포는 어디인가?

A: `neurons`가 수정 주 레포다. `neurons-ops`는 수정된 이미지를 배포하는 운영 레포이고, `stocks`는 소비자이므로 핵심 수정 대상이 아니다.

### Q: 왜 현재 Kubernetes Pod IP 기반 Host 허용만으로 부족한가?

A: MCP HTTP 서버가 Pod IP로 bind되면 기본 allowed host가 현재 Pod IP authority에 묶인다. 안정 외부 주소는 ingress/proxy/Tailscale HTTPS authority로 접근하므로, Pod가 재생성되어 IP가 바뀌어도 외부 Host header가 허용되어야 한다.

### Q: DNS rebinding 보호를 끄면 빠르지 않은가?

A: 끄지 않는다. 이 서버는 bearer 인증 없이 네트워크 경계를 핵심 방어선으로 쓰므로 `TransportSecuritySettings.enable_dns_rebinding_protection`은 계속 켠다. 필요한 추가 authority만 명시 allowlist로 확장한다.

### Q: 추가 Host 입력은 어떤 운영 표면을 가져야 하나?

A: CLI 반복 flag와 env CSV를 모두 지원한다. 운영 배포는 env 주입이 자연스럽고, 로컬/수동 smoke는 CLI가 간단하다. 두 입력은 additive이며 중복은 제거한다.

### Q: Origin은 어떻게 다룰까?

A: 추가 allowed host마다 HTTPS origin을 함께 허용한다. 외부 안정 주소는 TLS 종단 authority이므로 `https://<host>` 형태가 맞고, 기존 bind host의 직접 HTTP origin 허용은 그대로 유지한다.

### Q: 공개 레포에 실제 운영 hostname을 넣어도 되는가?

A: 안 된다. 문서와 테스트는 placeholder 또는 reserved test domain만 사용한다. 실제 tailnet hostname, private host, secret, raw dataset/document id는 public repo에 남기지 않는다.

## 기능 요구사항

- `neuron-knowledge mcp-http`는 `--allowed-host`를 반복해서 받을 수 있어야 한다.
- `MCP_HTTP_ALLOWED_HOSTS`는 comma-separated 추가 Host/authority 목록을 받을 수 있어야 한다.
- Compose MCP service는 brain env file의 `MCP_HTTP_ALLOWED_HOSTS`를 빈 project env 값으로 덮지 않아야 한다.
- Compose project env의 `MCP_HTTP_ALLOWED_HOSTS`가 비어 있지 않으면 brain env file 값보다 우선해야 한다.
- CLI flag와 env 값은 `TransportSecuritySettings.allowed_hosts`에 기존 bind host/port allowlist와 함께 추가되어야 한다.
- 추가 allowed host마다 `https://<authority>` origin이 `TransportSecuritySettings.allowed_origins`에 추가되어야 한다.
- 입력값은 Host header authority로만 받아야 하며 scheme, path, query, fragment, userinfo, wildcard는 거부해야 한다.
- Port는 ASCII digit-only여야 하며, IPv6 literal은 bracket form만 허용해야 한다.
- 중복 입력은 순서를 보존하며 한 번만 반영해야 한다.
- 기존 loopback, Tailscale tailnet, Kubernetes Pod IP bind guard는 유지해야 한다.
- base worker install은 optional `mcp-http` extra 없이도 계속 동작해야 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 보안 | DNS rebinding 보호는 유지하고 allowlist만 확장 |
| 공개 안전 | 실제 운영 host, private path, token, raw id를 문서/테스트/코드에 기록하지 않음 |
| 호환성 | 기존 CLI 인자와 기본 allowed host/origin 동작 유지 |
| 검증 | `uv run --extra mcp-http` 기반 MCP HTTP 테스트로 optional transport를 실제 실행 |
| 범위 | `neurons` 코드/테스트/문서만 수정하고 `neurons-ops` 배포 변경은 PR 범위 밖 |

## 사용자 시나리오

- 운영자는 MCP HTTP Pod를 Pod IP에 bind하되, 안정적인 외부 HTTPS authority를 env나 CLI로 추가해 Pod 재생성 뒤에도 MCP client Host/Origin 검증을 통과시킨다.
- 개발자는 optional extra를 켠 테스트에서 추가 Host와 HTTPS Origin이 실제 `TransportSecuritySettings`에 들어가는지 확인한다.
- 개발자는 compose contract test에서 project env alias와 brain env file fallback이 같은 canonical runtime env로 연결되는지 확인한다.

## 미결정 항목

- 없음. 운영값 주입은 `neurons-ops` 배포 단계에서 처리한다.
