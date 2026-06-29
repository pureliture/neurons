# Milestones — hermes-brain-steward

## M1 Provider identity 정규화 + Hermes ingest 수용
- status: done
- evidence: test_hermes_provider_identity.py 4 pass (canonicalize_provider 정규화,
  hermes ingest round-trip 저장, codex와 구분, casing 안정 identity); 기존 transcript/import
  회귀 50 pass. canonicalize_provider + transcript_parsers allowlist + PROVIDER_LANES hermes
  lane + document_model 정규화 적용.

## M2 Hermes proposer 귀속
- status: done
- evidence: test_hermes_brain_steward.py proposer 6 pass (기록/기본값/정규화/stale+supersede/
  no-leak/MCP dispatch round-trip); test_brain_steward 19 + neuron_mcp_stdio 회귀 pass.
  brain_steward proposer 인자 + steward_proposed_by stamp + _review_item.proposed_by +
  mcp_jsonrpc _steward_proposer + mcp_tools proposer 스키마(consumer enum 제약).

## M3 Read-only & proposal-only 회귀 가드 (Hermes 관점)
- status: done
- evidence: brain_context_resolve/brain_memory_search no-leak(steward proposal 미노출,
  consumer=hermes) pass; Korean free-text round-trip(redaction 한글 보존) pass; 기존
  candidate≠accepted/stale≠delete/supersede≠교체/authority-pack-only invariant(test_brain_steward,
  provider=hermes 픽스처) green.

## M4 Restricted 거부 명시 + write 응답 redaction
- status: done
- evidence: Hermes 기본 역할(allow_restricted=False)이 approve/reject/auto_accept 3종 모두
  거부·no-write pass; C4 restricted write 응답 public-safe projection(_safe_restricted_result)
  적용, source_refs/typed_payload/render_text 미노출 pass; 기존 restricted 테스트 회귀 green.

## M5 통합 회귀 + consumer contract + 전체 게이트
- status: done
- evidence: worker 전체 pytest 1289 passed / 9 skipped / 0 failed; consumer-contract
  parametrize(codex/claude-code/hermes) green; separation-manifest + server_boundary +
  repo_instructions 8 pass(신규 test_hermes_brain_steward.py sanitize 분류 추가);
  neuron-knowledge --show-boundary 정상; gradle test BUILD SUCCESSFUL.
