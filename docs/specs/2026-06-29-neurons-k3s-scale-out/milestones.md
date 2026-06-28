# Milestones — k3s-scale-out

approved design: `design.md`. TDD-first(red→green→refactor). 워크트리 `.worktrees/k3s-scale-out`.

## M1 워크로드 분류 + static YAML 누출 차단 게이트
- status: done
- evidence: K3sMigrationContractTest 신규 `workloadInventoryClassifiesScaleOutWithoutLeakingReplicaCounts`
  red(scaleCategory 0개)→green. inventory 14개 scaleCategory/replicaPolicy + config-contract replica-policy.
  gradle K3sMigrationContractTest 6/6 green.

## M2 manifest generator scale-out 경로 + 정수 가드
- status: done
- evidence: test_infra_baseline 신규 2개 red(ImportError)→green. infra_baseline.py에 reject_capacity_integers
  (key-scoped), scale_out_manifest_bundle, _hpa/_pdb/_statefulset/_headless_service/_pod_anti_affinity,
  _deployment_resource replica_policy. 기존 canary 테스트(replicas:1) 회귀 0. test_infra_baseline 11/11 green.

## M3 private 계약 + 선행조건 명문화
- status: done
- evidence: ops-overlay-contract 5종(agentNodeJoin/cniSelection/replicaCounts/hpaTargets/nodeSpecs),
  README scale-out 섹션(용량 숫자 미포함), inventory scaleOutPrecondition 주석.
  K3sMigrationContractTest --rerun-tasks 6/6 green(README/ops-overlay forbidden-substring 포함).

## M4 문서 + 파일 분류 + 전체 게이트
- status: done
- evidence: scale-out-runbook.md(redacted), spec 4파일 staged → docs/** catch-all 분류.
  전체 게이트 green — gradle test 전체 BUILD SUCCESSFUL, worker `uv run pytest -q` 1277 passed/9 skipped
  (test_separation_manifest 포함), `neuron-knowledge --show-boundary` 정상.
