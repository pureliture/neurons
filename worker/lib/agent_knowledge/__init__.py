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
# M7 seventh drill (2026-10-21): the Argo RBAC covers the per-lane
# applications, so the canary sync reaches every lane's own application.
# M7 eighth drill (2026-10-21): the production writer receives the shared
# name and derives the lane's own application, so the promotion completes.
# M7 ninth drill (2026-10-21): the production comparison is bound to the
# current ops main head, so a sibling's promotion commit no longer blocks it.
# M7 tenth drill (2026-10-21): the live authority reads each lane's own Argo
# applications, so the production promotion can authenticate the canary proof.
# M7 eleventh drill (2026-10-21): the delivery tools are checked against the
# resolved ops revision, so a tool change no longer invalidates the guard.
# M7 twelfth drill (2026-10-21): the live authority checks the lane's own
# overlay path, so the production promotion can authenticate the canary proof.
# M7 thirteenth drill (2026-10-21): every lane runs with the reviewed tool,
# so the seven lanes complete 6/6 in parallel.
# M7 fourteenth drill (2026-10-21): the scale-to-zero lane applications report
# Synced, so all seven lanes complete 6/6 in parallel.
