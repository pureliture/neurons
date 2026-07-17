# Co-located Python live delivery worker for rag-ingress-queue.
#
# Reuses the vendored agent-knowledge delivery subset + nats-py JetStream
# consumer. SAFE defaults: consumes the isolated RAG_INGRESS_SHADOW stream with
# delivery OFF. Consuming the live RAG_INGRESS_QUEUE requires the explicit
# ALLOW_LIVE_QUEUE=1 opt-in (set by compose for the live worker service), so a
# bare run never attaches to the live WorkQueue.
FROM python:3.13-slim

ARG NEURONS_SOURCE_COMMIT
RUN printf '%s' "${NEURONS_SOURCE_COMMIT}" | grep -Eq '^[0-9a-f]{40}$'
LABEL org.opencontainers.image.revision=${NEURONS_SOURCE_COMMIT}
LABEL org.opencontainers.image.source="https://github.com/pureliture/neurons"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV INGEST_STATE_DB_PATH=/var/lib/agent-knowledge/ingest-state/ingest-state.sqlite
ENV SHADOW_STREAM=RAG_INGRESS_SHADOW
ENV SHADOW_SUBJECT=rag.shadow.>
ENV SHADOW_DURABLE=shadow_python_worker
ENV SHADOW_DELIVER=0

WORKDIR /app

COPY pyproject.toml ./
COPY lib ./lib

RUN python -m pip install --no-cache-dir . \
    && python -m pip install --no-cache-dir "nats-py>=2.6" \
    # M6 dual-write (lean): qdrant-client to reach Qdrant + openai for the
    # OpenAI-compatible embedding endpoint. Docling is intentionally omitted --
    # mirror bodies are already-redacted markdown (Passthrough normalizer), so the
    # heavy doc-parser is not needed. Inert unless MIRROR_DUAL_WRITE=1 + QDRANT_URL.
    && python -m pip install --no-cache-dir "qdrant-client>=1.10" "openai>=1.0"

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/lib/agent-knowledge/ingest-state \
    && chown -R appuser:appuser /var/lib/agent-knowledge

USER appuser

# Long-running consume loop (max-messages omitted = run until stopped).
CMD ["python", "-m", "agent_knowledge.rag_ingress.shadow_worker"]
