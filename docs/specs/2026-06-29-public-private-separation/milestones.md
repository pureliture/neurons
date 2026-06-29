# Milestones — public/private separation remediation

## M1 separation-manifest + fail-closed contract test
- status: done
- evidence: deploy/separation/separation-manifest.json covers all tracked paths; worker/tests/test_separation_manifest.py 3 passed (fail-closed on unclassified); new files self-covered after commit.

## M2 sanitize current tree (+ .env.example / .gitignore / pyc)
- status: done
- evidence: tree leak-scan 0 hits; 12 kept files scrubbed (host alias/home/user paths); 8 invert-path files git rm'd from HEAD; 3 pyc untracked; .env.example RETIRED_INDEX_BRIDGE_*/PG/embedding stubs; worker 1253 passed; gradle test green.

## M3 leak-scanner tool + CI gate
- status: done
- evidence: scripts/separation_leak_scan.py (tree+history modes, runtime pattern file, allowlist); .github/workflows/leak-scan.yml (secret-gated fail-closed); tree scan CLEAN.

## M4 ops staging (neurons-ops, local, no push)
- status: done
- evidence: 8 private files staged to scratchpad/neurons-ops-staging + INVERT_PATHS.txt; no push.

## M5 history rewrite dry-run (main, throwaway clone, scan green)
- status: pending

## M6 [HUMAN GATE] cutover force-push + fork/cache + Tailscale rename
- status: pending (blocked: human approval)

## M7 policy-doc re-land (6fc366b -> clean main PR)
- status: pending
