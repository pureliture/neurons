# Hermes Brain Steward Server-Side Surface — Design Spec

## Overview

기존 proposal-only Brain Steward MCP surface(#42)와 Hermes read-consumer 인식 위에,
Hermes를 **ingest identity 평면의 1급 provider**로 붙이고 **proposal에 proposer 귀속**을
추가한다. Hermes는 read-only context와 proposal(candidate/stale/supersede)만 가능하고,
authoritative memory 확정(approve/reject/auto_accept)은 기본 권한에서 거부된다.

핵심은 zero-from-scratch가 아니라 **좁고 안전한 증분**이다: 이미 검증된 invariant
(candidate≠accepted, stale≠delete, supersede≠즉시교체, redaction fail-closed,
restricted default-denied)를 깨지 않고 Hermes provider를 정규화·수용·귀속한다.

## Requirements Reference

- Phase 1 source: `specs/hermes-brain-steward/requirements.md`
- Preview companion: `specs/hermes-brain-steward/requirements.html`
- 핵심 기능 요구사항: FR1(Hermes ingest identity), FR2(read-only authoritative-only),
  FR3(proposal + proposer 귀속), FR4(restricted 기본 거부), FR5(safety/redaction).

## Discovery 요약 (현재 상태, file:line 근거)

**이미 구축됨 (#42 / 기존):**
- 8개 steward 도구 전부 구현·배선·dispatch:
  `brain_steward.py:115-409`, 도구명/스키마 `mcp_tools.py:15-28`, dispatch
  `mcp_jsonrpc.py:89-202`(steward 라우팅 `:186-187`, `_dispatch_steward_tool :241-313`).
- restricted gate: `BrainStewardService(allow_restricted=False)` 기본,
  `_guard_restricted` → `StewardPermissionError`(`brain_steward.py:122-124, 287-291`),
  dispatch가 `permission:denied, write_performed:False`로 변환(`mcp_jsonrpc.py:302-313`).
  production은 항상 False(`cli.py`가 `allow_restricted_steward`를 안 넘김 `:128-166`).
- read 평면 accepted/current-only 구조적 보장: `_is_accepted_card`(`context.py:436`),
  `_is_accepted_ledger_card`(`brain_query.py:233-241`) — candidate/proposal은 read lane에
  도달 불가.
- redaction seam: `assert_public_safe`(`brain_steward.py:94-112`, 26개 forbidden key
  `:43-76`) + `_ensure_no_forbidden_content`(`memory_card.py`).
- Hermes read consumer: `CONTEXT_AUTHORITY_CONSUMERS={unspecified,codex,claude-code,hermes}`
  (`context_builder.py:16, 196-199`), 스키마 enum(`mcp_tools.py:111`), CLI(`cli.py:26`).
- 6/8 필수 invariant 테스트 존재(`test_brain_steward.py`), `provider="hermes"` 픽스처 사용.

**미구축 (이번 증분):**
- transcript ingest 평면이 `provider="hermes"`를 거부:
  `transcript_parsers.py:41` allowlist `{claude,gemini,codex,antigravity}` →
  `ValueError("unsupported provider: hermes")`; `historical_import.PROVIDER_LANES`(`:52`)에
  hermes 없음 → `UNKNOWN_PROVIDER`.
- `canonicalize_provider()` 부재: provider는 정규화 없이 passthrough,
  `build_session_id_hash=sha256(f"{provider}:{id}")`(`document_model.py:131-138`)라
  "Hermes"≠"hermes" identity drift 위험. (`canonicalize_project`는 존재 `transcript_model.py:35`.)
- proposal에 proposer actor 미기록: `_stamp_proposal`(`brain_steward.py:315-327`)이
  `proposed_by`를 남기지 않음. `source_span.provider`는 content metadata일 뿐 actor 아님.
- restricted write 응답 미-projection: `candidate_approve/reject`가 full card dict를
  `assert_public_safe` 없이 반환(`brain_steward.py:243-269`). 기본 Hermes는 restricted
  거부라 현재 누설은 아니나, human-gate 개방 시 누설 위험.

## Approach Proposal

### 추천: A — "정규화된 1급 provider + self-declared proposer 귀속 + 기존 flag 유지"

Hermes를 ingest 평면에 정규화하여 1급 provider로 수용(generic fixture 경로 재사용,
speculative native parser 없음)하고, proposal에 self-declared `proposer`(consumer와 동일
trust 모델)를 정규화·stamp한다. restricted 거부는 **기존 `allow_restricted=False`
production 기본값**을 그대로 쓰고, Hermes-role 명시 테스트로 고정한다. 새 RBAC/transport
auth를 만들지 않는다(코드베이스에 auth layer 부재, `mcp_http_server` bearer="YAGNI 미구현").

- 장점: 기존 invariant·테스트·dispatch seam을 그대로 재사용, 최소 표면, YAGNI 준수,
  필수 테스트 10개를 직접 충족.
- 단점: proposer는 self-declared(advisory)라 위조 가능 — 단 production transport가
  read-only ledger라 write는 어차피 fail-closed. 인증된 proposer는 transport auth가 생길 때
  후속 hardening.

### Alternative B — "transcript ingest 미변경, proposal-only identity"

`transcript_parsers`/`PROVIDER_LANES`를 건드리지 않고 Hermes를 read consumer + proposal
provider로만 취급. `canonicalize_provider`만 defense-in-depth로 추가.
- 기각 이유: 필수 테스트 #1 "provider=hermes ingest identity가 저장된다"와 implementation
  scope #1 "provider/source 값 hermes 수신 / 구분 저장 / 정규화 / ingest schema 호환"을
  ingest 평면에서 literally 충족하지 못함. Hermes 세션이 ingest되지 않으면 identity 저장이
  공허해진다.

### Alternative C — "request-scoped 인증 caller identity + per-call RBAC"

dispatch에 `caller_identity` 인자를 주입하고 HTTP는 `X-Caller-Identity` 미들웨어, stdio는
`initialize` handshake 파싱으로 per-request 역할을 강제.
- 기각(연기) 이유: 단일 transport=단일 role인 현 현실에서 over-engineering. 미들웨어+handshake
  파싱+4계층 시그니처 변경+credential 검증이 필요. 두 privileged role이 한 endpoint를
  공유하게 될 때 재검토.

**선택: A.** B의 정규화 아이디어를 흡수하되 ingest 수용까지 포함하고, C의 RBAC는 미래로 연기.

## Architecture

```text
[Hermes provider session] ──(generic provider_transcript_fixture.v1)──▶ ingest 평면
        │                                                                   │
        │  provider="hermes" (canonicalize_provider로 정규화)               ▼
        │                                            transcript_parsers.parse_transcript_source
        │                                            (allowlist += hermes, generic fixture 경로)
        │                                                                   │
        │                                            historical_import.PROVIDER_LANES[hermes]
        │                                                                   ▼
        │                                            CouchDB source / session memory
        │                                            (build_session_id_hash: 정규화된 provider)
        ▼
[Hermes MCP client] ──▶ agent_memory MCP server (stdio/http, 동일 dispatch seam)
        │
        ├─ read:  brain_context_resolve(consumer=hermes) / brain_memory_search
        │           └─ accepted/current-only (구조적 보장, 회귀 테스트로 고정)
        │
        ├─ read:  memory_authority_pack_read / memory_review_queue_list
        │           └─ assert_public_safe (redaction fail-closed)
        │
        ├─ proposal: memory_candidate_create / memory_stale_mark / memory_supersede_propose
        │           └─ proposer="hermes" stamp(steward_proposed_by), non-accepted lifecycle만
        │
        └─ restricted: memory_candidate_approve / reject / auto_accept
                    └─ allow_restricted=False(production) → permission:denied, write 없음
```

경계 규칙:
- ingest 평면의 `provider`(세션 출처)와 read 평면의 `consumer`(요청 주체)와 proposal의
  `proposer`(제안 actor)는 **서로 다른 식별 축**이다. 정규화 함수는 공유하되 의미를 섞지 않는다.
- authoritative memory = accepted+current MemoryCard만. proposal은 항상 non-accepted lane.
- 새 lifecycle/validation 규칙을 만들지 않고 기존 MemoryCard envelope/promotion 재사용.

## Data Flow

### Flow 1: Hermes 세션 ingest identity
1. Hermes provider 세션이 `provider="hermes"`, `provider_transcript_fixture.v1` payload로 도착.
2. `canonicalize_provider("Hermes"|"HERMES"|" hermes ") → "hermes"` (단일 정규화 지점).
3. `parse_transcript_source(provider="hermes", ...)`가 allowlist 통과 → generic
   `_parse_provider_fixture` 경로로 TranscriptSession/Chunk 생성.
4. `build_session_id_hash("hermes", raw_id)`로 codex/claude와 구분되는 안정적 identity 저장.
5. 결과: provider=hermes 세션이 저장되고 Codex/Claude와 구분된다(test #1).

### Flow 2: Hermes read (authoritative-only)
1. Hermes가 `brain_context_resolve(consumer="hermes")` 또는 `brain_memory_search` 호출.
2. read 평면이 accepted/current MemoryCard만 반환(`_is_accepted_*`). candidate/proposal 제외.
3. `consumer_contract = {consumer:"hermes", read_only:True, mutation_allowed:False}`.
4. 결과: Hermes는 candidate/proposal을 정답처럼 받지 않는다(test #9 회귀 가드).

### Flow 3: Hermes proposal + proposer 귀속
1. Hermes가 `memory_candidate_create(..., proposer="hermes")` 호출.
2. `candidate_create`가 후보 생성 → `_stamp_proposal`이 `steward_proposed_by="hermes"`
   (정규화) stamp, `mem_steward_` prefix memory_id 재발급.
3. `_persist_proposal`이 accepted 충돌·forbidden-content를 fail-closed 검증 후 non-accepted로 저장.
4. `_review_item`이 `proposed_by`를 안전 label로 노출(raw 식별자/secret 아님).
5. 결과: proposal은 review queue에만 보이고 authority pack엔 없으며 proposer=hermes 기록(test #8).

### Flow 4: Hermes restricted 거부
1. Hermes가 `memory_candidate_approve` 호출.
2. production service는 `allow_restricted_steward=False` → `_guard_restricted` raise →
   dispatch가 `permission:denied, write_performed:False` 반환. 어떤 write도 없음.
3. 결과: 기본 Hermes 권한에서 restricted 거부(test #7).

## Component Details

### C1. `canonicalize_provider` (신규)
- 위치: `worker/lib/agent_knowledge/session_memory/transcript_model.py` (`canonicalize_project` 옆).
- 입력: `provider: str` → 출력: `provider.strip().lower()` (빈 값은 빈 값 유지).
- 의존: 없음(순수 함수). 적용: `TranscriptSession.__post_init__`(`:144`),
  `TranscriptChunk.__post_init__`(`:204`), 필요 시 `TranscriptToolEvidence`(`:271`).
- 호환성: codex/claude/gemini/antigravity는 이미 lowercase → no-op. 회귀 없음.

### C2. Hermes ingest 수용 (수정)
- `transcript_parsers.py:41` allowlist에 `"hermes"` 추가. Hermes는 native JSONL parser 없이
  generic `_parse_provider_fixture`(`provider_transcript_fixture.v1`) 경로 사용.
  (Hermes 전용 raw 포맷이 생기면 그때 native parser 추가 — 지금은 speculative이므로 미작성.)
- `historical_import.py:52 PROVIDER_LANES`에 `hermes` 레인 추가(기존 lane 구조 미러,
  `parse_transcript_source`로 라우팅). unknown_provider 회피.
- 입력: provider="hermes" + fixture payload. 출력: 저장된 세션 identity. 의존: C1.

### C3. Proposer 귀속 (수정)
- `BrainStewardService.candidate_create/stale_mark/supersede_propose`에 `proposer: str = "unspecified"`
  추가, `canonicalize_provider`로 정규화.
- `_stamp_proposal`이 `card["steward_proposed_by"]=proposer` stamp.
- `_review_item`이 `"proposed_by": str(card.get("steward_proposed_by") or "unspecified")` 노출.
- dispatch(`_dispatch_steward_tool`)와 도구 스키마(`mcp_tools.py` `_STEWARD_SOURCE_SPAN_*`)에
  optional `proposer` 인자 추가(required 아님 — 기존 호출 호환).
- trust 모델: self-declared advisory(=consumer와 동일). 인증 proposer는 transport auth 후속 hardening.

### C4. Restricted write 응답 redaction (수정, safety hardening)
- `candidate_approve`/`candidate_reject` 반환 dict를 `assert_public_safe`로 통과시킨다
  (현재 full card 직접 반환 `:243-269`). auto_accept도 동일 점검.
- 기본 Hermes는 restricted 거부라 현재 누설 경로는 없지만, human-gate 개방 시 fail-closed 보장.

### C5. 회귀 테스트 (신규, 기존 invariant 고정)
- `test_brain_steward.py` 패턴(inline `_ledger`/`_span`/`_service`/`_text`) 미러.
- `test_neuron_mcp_stdio.py:964-1024` consumer-contract parametrize 패턴 미러.

## Error Handling

| 시나리오 | 대응 |
| --- | --- |
| 미지원 provider ingest | `transcript_parsers`가 `ValueError("unsupported provider: X")` — hermes는 허용으로 전환, 그 외는 유지 |
| provider casing drift | `canonicalize_provider`로 정규화 후 hashing — "Hermes"/"hermes" 동일 identity |
| proposal이 accepted lane 침범 | `_persist_proposal`이 fail-closed raise(기존) |
| read-only ledger에 write 시도 | `_guard_writable`이 명확한 메시지로 raise(기존) |
| restricted 호출(기본 권한) | `permission:denied, write_performed:False`, write 없음(기존) |
| forbidden content/path/secret 노출 | `assert_public_safe`가 write·read 모두에서 fail-closed(기존+C4 확장) |
| 한국어 free-text redaction 손상 | Korean round-trip 테스트로 `redact_*`가 Unicode 보존함을 고정 |

## Testing Strategy

러너: `cd worker && uv run pytest -q` (testpaths=`tests`, pythonpath=`lib,eval`, conftest 없음 —
inline `tmp_path` 픽스처). 관련 테스트 우선, 마지막에 worker 전체.

필수 테스트(완료 게이트) — 신규는 ★:
1. ★ provider=hermes ingest identity 저장 + Codex/Claude와 구분(C1+C2): hermes 세션 ingest →
   저장·session_id_hash 구분; `canonicalize_provider("Hermes")=="hermes"`.
2. candidate create ≠ accepted memory (기존 `:114` 회귀).
3. stale mark ≠ delete/mutate target (기존 `:143` 회귀).
4. supersede propose ≠ 즉시 winner 교체 (기존 `:174` 회귀).
5. review queue raw/private 미반환 (기존 `:235` 회귀).
6. authority pack accepted/current-only (기존 `:214` 회귀).
7. ★ restricted approve/reject/auto_accept가 기본 Hermes 권한에서 거부(`:298` 패턴, Hermes-role 명시).
8. ★ proposal에 proposer=hermes 기록 + 안전 label만 노출(C3).
9. ★ brain_context_resolve/brain_memory_search가 candidate/proposal을 authoritative로 미누설
   (consumer=hermes 포함, 회귀 가드).
10. ★ 기존 Codex/Claude read·ingest path 회귀 없음 + consumer-contract parametrize
    (`test_neuron_mcp_stdio.py:964` 패턴).
11. ★ Korean free-text round-trip(candidate_create→review_queue_list)에서 redaction 통과·텍스트 보존.

검증 보조: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 Java ingress
schema/boundary 가드가 영향받을 때만(provider는 Java측 free-form이라 영향 적음). 우선 worker
pytest로 게이트.

## TDD Strategy

code-changing 작업이므로 milestone마다 red → green → refactor:
1. user-visible 동작에 대한 실패 테스트 먼저 작성(예: hermes ingest, proposer 기록).
2. 최소 seam만 추가/수정(C1~C4)해 green.
3. 기존 invariant·boundary 테스트가 계속 green인지 확인(회귀 가드).
4. green 이후에만 refactor.
5. `uv run pytest`로 관련 → 전체 순으로 실행.

docs-only 예외 없음(코드 변경 작업). spec 문서 자체는 substitute evidence로 self-review.

## Milestones

agentic-execution이 act→observe→adjust로 소비하는 검증 단위. 각 milestone은 TDD-first.
**공통 게이트(모든 milestone):** 새로 tracked되는 파일(테스트/모듈/spec)은
`deploy/separation/separation-manifest.json`에 분류 추가 — 누락 시 fail-closed
`test_separation_manifest.py` 깨짐. main 직접 수정·live mutation 금지.

### M1: Provider identity 정규화 + Hermes ingest 수용
- done: `canonicalize_provider` 추가·적용(C1); `transcript_parsers` allowlist + `PROVIDER_LANES`에
  hermes 추가(C2); test #1 green; 기존 codex/claude/gemini/antigravity ingest 테스트 회귀 없음(#10 일부).
- evidence: hermes 세션 ingest round-trip 테스트 통과, `canonicalize_provider` 단위 테스트 통과,
  기존 transcript/provider 테스트 green.

### M2: Hermes proposer 귀속
- done: 3개 proposal 도구 + dispatch + 스키마에 `proposer` 추가(C3); `steward_proposed_by` stamp;
  `_review_item.proposed_by` 안전 노출; test #8 green; `assert_public_safe`로 proposed_by 안전 확인.
- evidence: proposer=hermes round-trip 테스트, review_queue redaction 테스트(#5) 계속 green.

### M3: Read-only & proposal-only 회귀 가드 (Hermes 관점)
- done: test #2,#3,#4,#6,#9 green(기존 invariant를 Hermes 맥락으로 고정); #9에서 누설
  발견 시 read 평면 필터 수정. Korean round-trip(#11) green.
- evidence: candidate≠accepted/stale≠delete/supersede≠교체/authority-pack-only/read-no-leak
  테스트 + Korean redaction 테스트 통과.

### M4: Restricted 거부 명시 + write 응답 redaction
- done: test #7(Hermes-role restricted denied) green; C4(restricted write 응답 assert_public_safe)
  적용; restricted 기본 거부가 production default(flag=False)로 보장됨을 테스트로 고정.
- evidence: Hermes approve/reject/auto_accept 거부·no-write 테스트, restricted write 응답 redaction 테스트.

### M5: 통합 회귀 + consumer contract + 전체 게이트
- done: consumer-contract parametrize(codex/claude-code/hermes) 테스트(#10) green;
  `cd worker && uv run pytest` 전체 green; separation-manifest/boundary 테스트 green;
  필요 시 gradle test.
- evidence: worker 전체 pytest pass 로그, manifest/boundary green, 변경 파일·잔여 follow-up 보고.

## Open Questions

- Hermes raw 세션의 실제 포맷: 현재는 generic `provider_transcript_fixture.v1`로 가정. Hermes가
  고유 native JSONL 포맷을 갖게 되면 `_parse_hermes_native_jsonl` 추가 — 본 milestone 범위 밖(YAGNI).
- 인증된 proposer/caller identity: transport auth(`mcp_http_server` bearer 미구현)가 생기기 전엔
  proposer는 self-declared advisory. per-request RBAC(Approach C)는 두 privileged role이 한
  endpoint를 공유할 때 재검토.
- review_queue의 proposer/proposal_kind 필터: 유용하나 본 범위 밖. `steward_proposed_by` 컬럼이
  생긴 뒤 후속.
- roadmap SoT 정정: `context-authority-roadmap/design.md`의 "Hermes read-only / proposal loop
  out of scope" 문구는 본 증분과 충돌. 구현 중 boundary 회귀 테스트가 Hermes proposal을 금지하면,
  "Hermes는 authoritative write 불가(proposal은 허용)"로 SoT를 정정하는 것은 grill-to-spec 상류
  회귀 사안(execution 루프에서 임의 수정 금지).

## Handoff to Agentic Execution

이 design.md 승인 후 구현은 `agentic-execution`이 one long-running goal으로 소비한다.
M1~M5 evidence 게이트를 따른다. 구현 중 requirements/design SoT 변경이 필요하면
execution 루프에서 고치지 않고 `grill-to-spec` 상류로 회귀한다.
