# Compose Env Anchor Cleanup First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: root `compose.yaml` RetiredIndexBridge env sharing and source-level guards
- live runtime mutation: 없음

## 확인한 drift

1. Java ingress services used an `x-ingress-java-env` anchor for RetiredIndexBridge env, but Python `ingress-worker-py` repeated the same RetiredIndexBridge base/API/dataset env directly.
2. The Java anchor only included five dataset env keys, while the Python worker block had all seven target-profile dataset keys.
3. Existing tests checked many compose strings and `.env.example` required-var coverage, but not whether RetiredIndexBridge env was shared through one anchor.

## 적용한 guard

- `x-retired-index-bridge-env` now owns the shared RetiredIndexBridge base URL, API key, and seven per-profile dataset ids.
- `x-ingress-java-env` merges that shared anchor and keeps Java-only NATS, delivery-enabled, and pressure-threshold env.
- `ingress-worker-py` merges the shared RetiredIndexBridge anchor while keeping Python-only live/shadow queue, state DB, pressure URL, Qdrant, and embedding env local to the service.
- `ComposeConfigTest.retiredIndexBridgeEnvAnchorIsSharedByJavaAndPythonWorkers` guards:
  - common anchor declaration
  - Java/Python merge usage
  - common RetiredIndexBridge env keys are directly declared once in `compose.yaml`
  - Python worker does not re-declare the common keys in its service block

## 검증

- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests "com.local.ragingressqueue.runtime.ComposeConfigTest.retiredIndexBridgeEnvAnchorIsSharedByJavaAndPythonWorkers"`
  - RED: missing shared anchor before compose change
  - GREEN: shared anchor after compose change
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests "com.local.ragingressqueue.runtime.ComposeConfigTest"`
  - 통과
- `cd worker && uv run python - <<'PY' ...`
  - parsed `compose.yaml` with PyYAML and confirmed `ingress-api`, `ingress-worker`, and `ingress-worker-py` all resolve the shared RetiredIndexBridge env keys after YAML merge

## 남은 리스크

- No live Docker Compose stack was started or mutated.
- The local `docker` binary did not expose `docker compose`; `docker compose -f compose.yaml config --quiet` could not run in this environment.
- `ComposeConfigTest` remains mostly string-based. A future hardening slice can replace the anchor assertions with a SnakeYAML-based service-env merge check in Java.
