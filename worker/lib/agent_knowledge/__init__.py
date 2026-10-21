__all__ = ["__version__"]

__version__ = "0.1.0"  # 2026-10-14: 자동 배포 체인 드릴 웨이브5 (session replica 계약 수정 검증)

# M7 parallel drill (2026-10-21): verifies that all seven delivery lanes
# start at once through GoCD's native publish trigger after the per-lane Argo
# application split. Comment only - no behaviour change.
# M7 second drill (2026-10-21): verifies the corrected lane-application
# handoff contract - all seven lanes start together and complete 6/6.
# M7 third drill (2026-10-21): the adapter is pinned by blob, so the lanes
# run the reviewed handoff contract and can complete in parallel.
# M7 fourth drill (2026-10-21): every lane starts together and completes 6/6
# with the canary sync accepting a sibling's newer ops main.
# M7 fifth drill (2026-10-21): the authority admits the per-lane canary
# paths, so the seven lanes complete their canary sync in parallel.
# M7 sixth drill (2026-10-21): the delivery tools are pinned by blob, so the
# seven lanes run the reviewed handoff contract and complete in parallel.
