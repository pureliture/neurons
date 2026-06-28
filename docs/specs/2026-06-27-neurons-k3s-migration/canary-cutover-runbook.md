# Canary/Cutover Runbook

이 runbook은 `compose 유지 + k3s canary` 전환을 위한 public-safe gate를 정의한다.
실제 live target, route, secret, approval evidence는 private `neurons-ops` repo가 소유한다.

## Order

1. Public contract validation passes.
2. Kubernetes client dry-run passes for the generated k3s artifacts.
3. Backup/restore rehearsal passes for stateful stores.
4. Operator requests explicit approval for server dry-run against the live cluster.
5. Kubernetes server dry-run passes.
6. Operator requests explicit approval for k3s canary live apply.
7. WorkQueue isolation is confirmed: shadow stream, separate durable, or worker disabled
   health-only validation. Sharing the compose live durable is forbidden.
8. kube-apiserver operator allowlist and NetworkPolicy expectations are confirmed.
9. k3s canary starts without taking full primary traffic.
10. Health, API/MCP behavior, worker behavior, and stateful readiness pass.
11. Operator requests explicit approval for read/write canary.
12. read/write canary runs with public-safe synthetic data only.
13. Cutover approval is requested with redacted evidence.
14. After cutover postcheck passes, compose retire is requested as a separate approval gate.

## Canary Checks

- service readiness
- redacted health endpoints
- API/MCP behavior
- worker behavior
- worker uses shadow stream, separate durable, or disabled health-only validation
- stateful dependency readiness
- backup/restore evidence reference
- rollback path to compose primary
- public-safe synthetic read/write canary after explicit approval
- max 24h safety window with abort-to-compose-primary if no decision is reached
- kube-apiserver access allowlist and NetworkPolicy expectations

## Abort Criteria

- dry-run failure
- missing private overlay input
- Tailscale route broader than approved
- kube-apiserver access not allowlisted
- NetworkPolicy expectation missing before promotion
- canary worker shares the compose live durable
- canary window exceeds 24h without promotion or rollback decision
- backup/restore rehearsal failure
- canary health failure
- read/write canary failure
- evidence leak or unredacted private data
- rollback path cannot be confirmed

## Rollback

- Keep compose primary during canary.
- If canary fails, stop promotion and preserve compose as the authority.
- If no promotion or rollback decision is reached within 24h, abort to compose primary.
- If cutover has occurred and postcheck fails, use the approved rollback procedure from the
  private ops repo.
- Do not stop compose until the compose retire approval gate is recorded.

## Evidence Rules

Allowed:

- pass/fail gate status
- sanitized service names
- redacted health summaries
- public-safe synthetic canary summaries
- command role and outcome

Forbidden:

- raw transcript bodies
- secret values
- private filesystem paths
- raw dataset identifiers
- raw document identifiers
- full environment dumps
