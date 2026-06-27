# Architecture Modernization Campaign Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`

## 입력 맥락

- 시작 문서: `/Users/ddalkak/Projects/neurons/docs/architecture-review-summary.md`
- 요약된 아키텍처 축:
  - Database dialect isolation, Repository pattern, Unit of Work
  - Immutable external configuration for dataset contracts
  - `CurationService` 같은 서비스 계층의 transaction boundary

## 질문-답변 흐름

### Q: 이번 grill-to-spec의 첫 요구사항 범위는 무엇인가?

전체 아키텍처 리뷰를 상위 로드맵으로 다룬다. DB/UoW 리팩터링과 dataset contract 설정화를 하나의 modernization 흐름 안에서 정리하고, 이후 실행 가능한 단위로 나눌 수 있게 만든다.

### Q: 로드맵이 작업 수행까지 포함하는가?

포함한다. 이 산출물은 단순한 상위 로드맵이 아니라 실행 포함 campaign 기준 문서가 되어야 한다. 승인된 `design.md`는 여러 workstream을 순서대로 구현할 수 있는 evidence-oriented milestone을 포함하고, 각 milestone은 구현 담당자가 `agentic-execution`으로 수행할 수 있을 만큼 완료 기준과 검증 기준을 제공해야 한다.

### Q: 3번 방향으로 갈 때 첫 milestone은 무엇인가?

첫 milestone은 `M1: Ledger persistence safety harness and UoW seam`으로 확정한다. 바로 `ledger.py`를 크게 분해하지 않고, 먼저 현재 DB behavior를 characterization tests로 고정하고, 기존 호출자와 runtime behavior를 유지한 채 `UnitOfWork` / Repository interface가 들어갈 얇은 seam을 만든다. 실제 DB migration이나 큰 책임 이동은 M1 범위가 아니다.

### Q: M1에서 제일 먼저 테스트로 고정할 behavior surface는 무엇인가?

M1의 첫 safety surface는 transaction failure safety로 확정한다. 중간 실패가 partial write나 깨진 persistence state를 남기지 않는다는 점을 먼저 characterization tests로 고정한다. 이 안전망은 UoW seam을 도입하는 이유를 직접 증명해야 하며, 상태 전이/DB dialect/service 호출 호환성은 이후 같은 M1 안에서 보조 surface로 확장한다.

### Q: transaction failure safety는 어느 레벨에서 먼저 증명할 것인가?

Ledger 직접 레벨을 먼저 고정하고, 그 다음 Service 레벨로 확장한다. M1은 낮은 레벨 persistence API에서 실패 주입과 rollback 성격을 먼저 증명한 뒤, `CurationService` 같은 실제 use case 호출 흐름에서도 같은 실패 안전성이 깨지지 않는지 확인해야 한다.

### Q: campaign 전체 성공 기준은 어디까지인가?

campaign 성공 기준은 M1을 실제 구현하고, 후속 workstream을 바로 이어갈 수 있는 실행 준비 상태까지 만드는 것이다. 즉 `Ledger persistence safety harness and UoW seam`은 구현과 검증까지 완료해야 하며, Repository extraction과 dataset contract/config workstream은 다음 milestone으로 바로 착수할 수 있을 만큼 gate, done 정의, expected evidence가 정리되어야 한다.

### Q: 후속 workstream 준비 깊이는 어디까지인가?

후속 workstream은 거의 design 수준까지 준비한다. Phase 1에서는 세부 아키텍처를 확정하지 않지만, 승인될 `design.md`는 M2/M3 구현자가 바로 들어갈 수 있을 만큼 목적, 범위, component/data-flow 후보, done 정의, test evidence, rollback/abort 기준을 구체화해야 한다.

### Q: runtime/deployment evidence는 어디까지 요구할 것인가?

runtime/deployment evidence는 로컬 worker test와 read-only Ubuntu runtime check로 제한한다. M1은 persistence safety harness와 seam 도입이므로 live mutation, Docker/systemd 변경, credential 접근, RAGFlow write/delete/disable, 운영 DB 변경은 포함하지 않는다. 다만 `neurons`의 실제 runtime truth는 Ubuntu surface이므로, 구현 완료 전후에 read-only 상태 확인을 통해 배포 표면과 충돌하지 않는지 확인한다.

## 기능 요구사항

- `neurons`의 server/brain-side 책임을 기준으로 architecture modernization campaign 범위를 정의한다.
- DB/UoW/Repository 리팩터링과 dataset contract 설정화를 하나의 실행 campaign 안에서 비교 가능한 workstream으로 정리한다.
- campaign은 실제 구현 순서, 안전 gate, 테스트 evidence, runtime activation boundary를 판단할 수 있을 만큼 구체적이어야 한다.
- 승인된 `design.md`는 개별 workstream 구현을 수행할 수 있는 milestone과 done 정의를 포함해야 한다.
- 각 workstream은 필요하면 더 작은 하위 spec으로 회귀할 수 있지만, 기본 목표는 이 campaign spec 하나로 순차 실행 가능한 수준까지 합의하는 것이다.
- 첫 implementation milestone은 `Ledger persistence safety harness and UoW seam`으로 시작한다.
- M1의 첫 characterization target은 transaction failure safety로 둔다.
- M1의 transaction failure safety는 Ledger 직접 레벨을 먼저 증명하고 Service 레벨로 확장한다.
- campaign 성공은 M1 구현 완료와 후속 workstream 실행 준비를 함께 요구한다.
- 후속 workstream은 `design.md`에서 구현 직전 수준의 구체성을 가져야 한다.
- runtime/deployment evidence는 로컬 worker test와 read-only Ubuntu runtime check로 제한한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Scope control | campaign은 실행을 포함하되, 각 milestone은 검증 가능한 단위로 작게 유지한다. |
| Compatibility | 기존 public CLI/API, 기존 DB behavior, 기존 dataset contract semantics를 암묵적으로 깨지 않는다. |
| Safety | live RAGFlow, Docker/systemd, credential, raw transcript/source mutation은 이 campaign 구현 범위에서 직접 실행하지 않는다. |
| Evidence | 각 workstream은 완료 판단에 필요한 test/runtime/document evidence를 명시해야 한다. |
| Ownership | `neurons`가 소유한 server/brain-side authority와 `dendrite` thin-client 책임을 섞지 않는다. |
| TDD-first | code-changing milestone은 red -> green -> refactor 또는 동등한 TDD-first 흐름을 기본값으로 둔다. |
| M1 blast radius | M1은 behavior 고정과 seam 도입에 집중하며, DB migration이나 대규모 책임 이동은 포함하지 않는다. |
| M1 first safety surface | 실패 중간 지점에서 partial write나 깨진 persistence state가 남지 않는 transaction failure safety를 먼저 고정한다. |
| M1 proof order | Ledger 직접 레벨 characterization을 먼저 만들고 Service 레벨 호출 흐름으로 확장한다. |
| Campaign success | M1은 구현과 검증까지 완료하고, 후속 workstream은 즉시 착수 가능한 gate와 evidence 기준까지 준비한다. |
| Follow-up depth | M2/M3는 `design.md`에서 구현자가 바로 착수할 수 있을 만큼 목적, 범위, 흐름, done 기준, test evidence를 구체화한다. |
| Runtime evidence | M1은 로컬 worker test를 중심으로 검증하고, Ubuntu runtime은 read-only 상태 확인만 수행한다. |

## 사용자 시나리오

- Maintainer가 architecture review 결과를 보고, modernization 작업을 어느 순서와 gate로 실제 수행할지 합의한다.
- 구현 담당자는 승인된 campaign `design.md`를 보고 첫 milestone부터 test-first로 진행한다.
- 구현 중 SoT 변경이 필요하면 임의로 범위를 확장하지 않고 이 requirements/design 단계로 회귀한다.
- 구현 담당자는 M1에서 기존 persistence behavior를 먼저 테스트로 고정한 뒤 UoW seam을 도입한다.
- 구현 담당자는 M1에서 실패를 주입했을 때 persistence state가 오염되지 않는지 먼저 증명한다.
- 구현 담당자는 Ledger API에서 실패 안전성을 확인한 뒤, Service 호출 흐름에서도 같은 보장을 확인한다.
- 구현 담당자는 M1을 완료한 뒤, Repository extraction과 dataset contract/config 작업을 이어갈 수 있는 기준을 남긴다.
- 후속 구현 담당자는 campaign `design.md`만으로 M2/M3 착수 여부와 검증 기준을 판단할 수 있다.
- 구현 담당자는 M1 검증에서 live mutation 없이 Ubuntu runtime surface를 read-only로 확인한다.

## 미결정 항목

- 없음.
