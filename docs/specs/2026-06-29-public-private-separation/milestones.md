# Milestones — public/private separation remediation

## M1 separation-manifest + fail-closed contract test
- status: done
- evidence: deploy/separation/separation-manifest.json covers all tracked paths; worker/tests/test_separation_manifest.py 3 passed (fail-closed on unclassified); new files self-covered after commit.

## M2 sanitize current tree (+ .env.example / .gitignore / pyc / .agents)
- status: pending

## M3 leak-scanner tool + CI gate
- status: pending

## M4 ops staging (neurons-ops, local, no push)
- status: pending

## M5 history rewrite dry-run (main, throwaway clone, scan green)
- status: pending

## M6 [HUMAN GATE] cutover force-push + fork/cache + Tailscale rename
- status: pending (blocked: human approval)

## M7 policy-doc re-land (6fc366b -> clean main PR)
- status: pending
