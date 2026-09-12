# LLM-Brain MCP Stable Host Rollout Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- 승인 상태: 사용자가 `requirements.md`와 후속 `design.md`를 사전 승인함.
- 실행 방식: 승인된 `design.md`를 단일 목표로 보고 `agentic-execution`으로 진행한다.

## 질문-답변 흐름

### Q: 이번 목표의 실제 종료 지점은 어디인가?

A: `neurons` 코드/image, `neurons-ops` k3s manifest, tyche 설정, 필요 시 Tailscale ACL 판정까지 rollout 준비를 닫는다. production cluster apply, ArgoCD sync/prune, Tailscale policy mutation, tyche live mutation은 별도 live 승인 게이트 전에는 실행하지 않는다.

### Q: MCP 서버가 무엇을 allowlist해야 하는가?

A: 클라이언트 기기가 아니라 stable Tailscale Serve server authority를 allowlist한다. `Host` header가 stable server URL authority와 일치하면 통과해야 하고, MacBook/Mac mini/iPhone 같은 client device name, client Tailscale IP, client MagicDNS 이름은 allowlist에 넣지 않는다.

### Q: DNS rebinding protection을 끌 수 있는가?

A: 끄지 않는다. `TransportSecuritySettings.enable_dns_rebinding_protection`은 계속 켜고, stable server authority만 명시 allowlist로 추가한다.

### Q: `neurons` public repo에 실제 운영 hostname을 기록할 수 있는가?

A: public-safe 문서와 테스트에는 실제 tailnet hostname을 기록하지 않는다. public repo에는 `<stable-tailscale-serve-host>` placeholder와 reserved test domain을 사용하고, 실제 운영값은 private `neurons-ops` manifest 또는 운영 secret/config 표면에서만 다룬다.

### Q: 기존 running image를 신뢰할 수 있는가?

A: 신뢰하지 않는다. running image가 stable Host allowlist 기능을 포함한다고 간주하지 않고, 현재 `neurons` commit 기준의 새 `mcp-http` image tag를 build/push한 뒤 `neurons-ops` manifest가 그 tag를 명시해야 한다.

## 기능 요구사항

- `neurons` MCP HTTP 서버는 `MCP_HTTP_ALLOWED_HOSTS` comma-separated authority 목록을 지원해야 한다.
- `neurons` MCP HTTP CLI는 반복 가능한 `--allowed-host`를 지원해야 한다.
- 추가 allowed host는 `TransportSecuritySettings.allowed_hosts`에 기존 bind host/port allowlist와 함께 반영되어야 한다.
- 추가 allowed host마다 `https://<authority>` origin이 `TransportSecuritySettings.allowed_origins`에 반영되어야 한다.
- wildcard, 빈 host, scheme/path/query/fragment/userinfo, public/private 전체 대역, client device identity allowlist는 금지한다.
- `0.0.0.0` bind 거부 정책은 유지한다.
- Kubernetes Pod IP bind 허용 정책은 유지한다.
- `neurons` focused MCP HTTP 테스트가 통과해야 한다.
- 새 `mcp-http` image tag는 현재 verified source commit에서 산출되어야 한다.
- `neurons-ops` canary와 production manifest는 새 image tag와 stable server authority allowlist를 명시해야 한다.
- tyche 설정은 production stable-host proof 후 Pod IP `Host` header workaround를 제거하는 방향이어야 한다.
- Tailscale ACL은 server stable URL 접근을 막는 증거가 있을 때만 변경 대상으로 올린다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| 보안 | DNS rebinding protection 유지, client 기기별 allowlist 금지 |
| 공개 안전 | public `neurons` repo에는 실제 tailnet hostname, private path, secret, raw id 기록 금지 |
| 배포 안전 | live apply/sync/restart/ACL mutation은 별도 승인 전 금지 |
| 증거 | 각 milestone은 테스트, render, image tag, kustomize output, 또는 read-only live evidence로 완료 판단 |
| 격리 | `main`/`master` 직접 수정 금지, repo별 전용 branch/worktree 사용 |

## 사용자 시나리오

- Tailnet에 등록된 client는 stable Tailscale Serve URL로 `/mcp`에 접근하고, MCP 서버는 client identity가 아니라 `Host` authority를 검증한다.
- Pod가 재시작되어 Pod IP가 바뀌어도 tyche/stocks는 Pod IP `Host` workaround 없이 stable URL로 계속 연결된다.
- 운영자는 canary에서 새 image와 allowlist를 먼저 확인한 뒤 production manifest와 tyche 설정을 순차적으로 반영한다.

## 미결정 항목

- Tailscale ACL 변경 필요 여부: current ACL/Serve evidence 확인 전까지 미정이며, 기본값은 변경하지 않는다.
- tyche config의 실제 저장 위치: local repo/config discovery 결과에 따라 patch 또는 운영 지침으로 처리한다.
