# 인계 프롬프트: RAGFlow 퇴역 후속 3건 통합 처리

아래 프롬프트는 하나의 세션에서 3건을 순차 처리하기 위한 것입니다. 그대로 새 세션에 전달하거나,
`/create-pr` 등 세션 시작 경로에 붙여 쓰면 됩니다.

---

## 반드시 지킬 순서 (이 순서가 정답입니다)

3건은 독립이 아닙니다. **코드 결정(1, 2) → 이미지 재빌드(3)** 순으로 가야 합니다.

```
[1] legacy RAGFlow 코드 잔존 처리 결정 + 구현
[2] evidence 이중 계층 구조 판단 + 필요한 변경
        ↓
[3] 라이브 이미지 3종 재빌드·재배포
```

**왜 이 순서인가:** `worker/Dockerfile`은 `COPY lib ./lib`로 worker 코드를 이미지 안 그대로 복사합니다.
1번과 2번이 `worker/lib/` 아래 코드를 바꾸므로, **3번을 먼저 하면 그 변경이 이미지에 안 들어가서
이미지를 두 번 빌드해야 합니다.** 코드 결정을 먼저 확정해야 재빌드가 한 번으로 끝납니다.

반대로 3번(재배포)을 마지막에 두면, 코드 변경 → 빌드 → 배포로 이어져 라이브가
최종 코드 상태가 됩니다.

---

## 인계 프롬프트

> neurons 저장소(pureliture/neurons)의 RAGFlow 퇴역 후속 작업 3건을 순차 처리한다.
> 두 저장소와 라이브 k3s 클러스터가 관여한다.
>
> **작업 저장소**
> - 코드: `~/Projects/neurons` (pureliture/neurons)
> - 배포 오버레이: `~/Projects/neurons-ops` (pureliture/neurons-ops) — 라이브 전용 배포 입력 소유
> - 라이브: k3s. `llm-brain-server`는 agent 노드이고 control plane은 `100.95.155.103`.
>   sudo 없이 Mac의 `~/.kube/oci-k3s-poc-config` kubeconfig로 실제 클러스터 조회/배포 가능
>   (이전 RAGFlow 정리 세션에서 검증된 경로).
>
> **완료된 선행 작업 (참고)**
> - PR #331 → main `7fbe61f`: antigravity tool 결과를 원문 stdout에서 "어떤 명령 + 결과" 요약으로 전환.
>   antigravity 파서가 stdout을 turn 텍스트에서 제거하고, tool_evidence lane이 호출-결과를 짝지어
>   압축 요약을 만들며, 그 요약이 Graphiti entity extraction 입력에 addendum로 편입된다.
> - PR #334 → main `546eaaa`: RAGFlow 프로토콜 잔존 정리. Java RAGFlow 어댑터 9개 +
>   `scripts/live-index-verify.py` 삭제, `rag_ingress/retired_index_bridge.py` → `index_adapter.py`
>   등 backend-neutral 명칭으로 중립화, `worker/lib/agent_knowledge/index_client.py` →
>   `legacy/index_client.py`로 격리, `couchdb_source/index_projector.py`·`index_fallback.py` 제거,
>   `SESSION_MEMORY_PROJECTION_BACKEND`의 코드 default를 `qdrant`로 변경.
> - 라이브 실측(앞선 세션): 라이브 secret 3개 키 전수 검사 결과 `RAGFLOW*`·`RETIRED_INDEX_BRIDGE*` 0건.
>   neurons-ops overlay 저장소 grep에서도 옛 이름 잔존 0건. 라이브 `INGRESS_DELIVERY_BACKEND=couchdb`,
>   `SESSION_MEMORY_PROJECTION_BACKEND=postgres_pgvector`, `SHADOW_DELIVER=1`.
>   **즉 서버 설정은 깨끗하다. 남은 건 "서버를 새 코드로 재시작"이다.**
>
> ---
>
> ### 1단계 — legacy RAGFlow 코드 잔존 처리
>
> `worker/lib/agent_knowledge/legacy/index_client.py` (32.2KB)가 남아있다. `legacy/__init__.py`의 계약은
> "운영자 전용 archive/GC 도구 전용이며 활성 런타임 경로에서 도달 불가"다. 그런데 실제로 import하는
> 모듈이 많다: `session_memory_gc.py`, `transcript_volume_gc.py`, `transcript_session_gc.py`,
> `transcript_backfill.py`, `gc_backup.py`, `cleanup_readiness.py`, `native_memory_writer.py`,
> `native_memory_reconcile.py`, `neuron_session_memory.py`, `knowledge_search_service.py`, `mcp_server.py`, `cli.py`.
>
> RAGFlow 서버는 이미 죽었고 라이브에 credential도 0건이라 **이 도구들은 현재 동작 불가 상태**다.
>
> 할 일:
> 1. `RetiredIndexBridgeHttpClient` 인스턴스를 실제로 만드는 지점과 그 메서드가 호출되는 실행 경로를
>    구분해 추적한다. import만 vestigial인지, 실제 접속 경로가 살아있는지.
> 2. 각 도구를 분류한다: (a) RAGFlow 접속이 필수라 지금 동작 불가 / (b) 접속이 선택이라 다른 백엔드로
>    대체 가능 / (c) 이미 vestigial이라 정리 가능.
> 3. `native_memory_*` 경로의 현재 제품 상태를 확인한다. 이게 외부 브리지에 직접 쓰는 기능이면,
>    RAGFlow F/O 이후 대체 백엔드(CouchDB / Postgres+pgvector / Qdrant) 중 어디로 갔는지, 아니면
>    아직 안 갔는지. 안 갔으면 그게 남은 제품 결정이다.
> 4. 정리한다: 지울 것 / 대체 백엔드로 옮길 것 / 유지할 것. 각 항목에 근거와 영향 범위(어떤 CLI·도구가
>    영향받는지)를 붙인다.
> 5. 이 단계는 제품 결정이므로 **사용자에게 결정 제안을 먼저 올리고 승인받은 뒤** 구현한다.
>
> ---
>
> ### 2단계 — evidence 이중 계층 구조 판단
>
> antigravity 압축 작업(PR #331) 결과로 tool evidence가 두 경로로 나뉘었다.
>
> | | recall 경로 | Graphiti entity pass |
> |---|---|---|
> | 진입점 | `pack_session_memory_document` (session_memory/memory_regeneration.py:1279) | `session_episode_from_couchdb_source` (llm_brain_core/runtime.py:714) |
> | 입력 | chunk prose + `## Tool Evidence` 섹션 | chunk prose(8000자) + evidence addendum(2000자) |
> | evidence 소스 | `list_tool_evidence_summaries` (ledger) | `resolved.tool_evidence_bundles` (CouchDB) |
> | 렌더링 | `### N cat/outcome`, `- command:`, `- result:` | 동일 + `- tool:` 줄 |
>
> 같은 evidence 레코드를 서로 다른 저장소에서 따로 읽어 각기 다른 문서에 넣는다.
>
> 판단할 것:
> 1. 이 이중 구조가 정당한가, 아니면 한쪽이 중복인가. 두 경로가 **같은 정보를 담는지, 시점·범위가
>    다른 정보를 담는지**를 코드로 확인한다. evidence 출처(ledger vs CouchDB)가 같은 원본을 가리키는지,
>    시점이 다른지, 커버 범위가 다른지.
> 2. 두 렌더링 형식을 하나의 렌더러로 통일할 수 있는가. 현재 각각
>    `memory_regeneration.py:_sectioned_tool_evidence_lines`와 `runtime.py:_extraction_text_from_evidence`가
>    따로 파싱한다.
> 3. recall 경로에 추가 조치가 필요한가. antigravity는 이제 stdout이 제거된 prose만 담고,
>    Claude/Codex/Gemini/Grok는 원래부터 tool 결과가 chunk에 안 들어가므로 provider 간 차이가 남는지 확인.
>
> 이것도 제품 결정이므로 **사용자에게 결론을 먼저 제시하고 승인받은 뒤** 구현한다.
> 참고: `docs/handoffs/tool-evidence-dual-plane-review.md`에 질문 3개와 판단에 필요한 사실관계가 정리돼 있다.
> 기록 형식은 `docs/retired/ragflow-protocol-residue-inventory.md`의 결정 표(제거/중립화/유지 + 근거)를 따른다.
> evidence 화이트리스트는 현행 유지가 이미 확정 사항이므로 재검토 대상이 아니다(근거는
> `session_memory/transcript_parsers/evidence.py`의 `_classify_exec_evidence` docstring).
>
> ---
>
> ### 3단계 — 라이브 이미지 재빌드·재배포
>
> 1·2단계의 코드 변경이 **모두 `worker/lib/` 아래에 있고 `worker/Dockerfile`이 `COPY lib ./lib`로
> 이미지 안에 복사한다.** 그래서 코드 변경이 확정된 뒤에 이미지 빌드해야 한 번으로 끝난다.
>
> 할 일:
> 1. 재빌드 대상 이미지 3종: `neurons-ingress-worker`, `neurons-session-memory-worker`,
>    `neurons-ingress-api`(canary 포함). neurons-ops의 `k3s/` 디렉토리에 ArgoCD·CI-CD·pipeline
>    구성이 있고 `docs/roadmaps/homelab-k3s-decommission/manifests/`에 워크로드 매니페스트가 있다.
>    실제 빌드·배포 절차를 먼저 파악할 것.
> 2. 각 이미지 빌드 → 라이브 클러스터에 재배포 → rollout 상태 확인.
> 3. `ingress-worker`의 기본 백엔드 값이 `retired_index_bridge` → `couchdb`로 바뀐 것이 런타임에
>    반영됐는지 확인.
> 4. `neurons-session-memory-worker`의 entrypoint가 gc/backfill lane을 스케줄하지 않는지 확인.
>    원본도 credential 미설정 시 skip이라 **라이브 동작 변화 없음**이어야 한다.
> 5. canary `/status` 문자열 변경 `pressure_read_failed` → `not_configured`이 downstream alert/eval에
>    영향 있는지 neurons-ops에서 먼저 확인하고, 있다면 갱신 대상으로 보고.
>
> **3단계는 라이브 mutation이다. 아래 제약을 반드시 지킬 것.**
>
> ---
>
> ## 제약 (전 단계 공통)
>
> - **라이브 변경은 사용자 explicit approval 없이 실행하지 않는다.** CLAUDE.md 가드레일:
>   "Retired external index bridge write/delete/disable, live GC, Docker/systemd/firewall/package
>   mutation은 current evidence, explicit user intent, exact argv, timeout, redaction, postcheck,
>   rollback/abort criteria 없이 실행하지 않는다."
>   → 근거 / 정확한 명령 / 타임아웃 / 사후 확인 / 롤백 기준을 모두 갖춰라.
> - 브리지 `delete_document`·`disable_document` 같은 라이브 mutation은 **절대 호출 금지**.
>   RAGFlow는 죽어있고 라이브에 credential도 없다.
> - 배포 전 현재 이미지 태그/digest를 기록해 롤백 기준 확보. 배포 후 pod 상태·로그 확인,
>   이상 시 이전 태그로 되돌릴 수 있어야 한다.
> - Mac에서 라이브 배포를 하지 말 것. 라이브 호스트에서 할 것.
> - secrets/token/raw dataset_id/raw document_id/raw transcript body를 로그·커밋·PR 본문에 절대 쓰지 말 것.
> - Git: main/master checkout에서 파일 변경 금지. `git worktree`를 쓸 것. 두 저장소 모두 해당.
>   커밋 메시지 마지막 줄 `Co-Authored-By: Claude <모델명> <noreply@anthropic.com>` 필수.
> - 응답과 문서는 한국어, 코드 식별자·CLI·파일명·명령어는 영어 원문.
> - 1·2단계는 **제품 결정이므로 사용자 승인 전에는 커밋하지 말 것.** 계획안만 먼저 제시한다.
>
> ---
>
> ## done 기준
>
> - 1단계: `legacy/index_client.py` 소비자가 (a)/(b)/(c) 중 어디인지 분류된 표 + native_memory 기능의
>   현재 제품 상태 + 정리 계획안(근거·영향 범위 포함) → 사용자 승인 후 구현
> - 2단계: 3가지 질문에 대한 판단과 코드 근거 → 사용자 승인 후 필요한 변경 반영
> - 3단계: 이미지 3종이 최종 코드로 재빌드·재배포되어 rollout 완료 + before/after 이미지 태그 +
>   확인 로그(redaction) + canary 문자열 영향 확인 + 롤백 필요 시 어떻게 되돌렸는지
> - 각 저장소에 필요한 변경이 있으면 그 저장소에 PR로 올림
> - `cd worker && uv run pytest -q` 통과

---

## 이 프롬프트를 사용할 때

- **한 세션으로 갈 경우**: 위 프롬프트 전체를 그대로 전달. 단계 1→2→3이 순서대로 진행된다.
- **neurons-ops 저장소에서 시작해야 하는 경우**(배포가 주 작업일 때): 세션 시작 시
  `cwd=~/Projects/neurons-ops`를 지정한다. 1·2단계는 코드 저장소에서 별도 worktree로 한다.
  단, `own_initiative` 카드에는 `cwd`를 못 넣으므로 이 경우 사용자가 직접 세션을 시작해야 한다.
- **병렬로 갈 경우**: 3단계는 1·2단계 완료 후에만 실행 가능하므로 실질적으로 직렬이다.
  evidence 이중 계층(2단계)만 1단계와 병렬 가능 — 두 건이 같은 파일을 건드리지 않는다면.
