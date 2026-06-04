package com.local.ragingressqueue.ingest.domain.validation;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.ingest.domain.TargetProfileRegistry;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public class IngestJobValidator {
    private static final String INLINE_PAYLOAD_KIND = "redacted_rag_ready_document";
    private static final String REDACTION_VERSION = "redaction.v2";
    private static final Set<String> DOCUMENT_KINDS = Set.of(
        "conversation_chunk",
        "tool_evidence_summary",
        "session_summary",
        "session_recap",
        "project_context_snapshot",
        "task_summary",
        "approved_memory_card",
        "repo_usage_pattern"
    );

    private final TargetProfileRegistry targetProfileRegistry;

    public IngestJobValidator() {
        this(TargetProfileRegistry.DEFAULT);
    }

    public IngestJobValidator(TargetProfileRegistry targetProfileRegistry) {
        this.targetProfileRegistry = targetProfileRegistry;
    }

    public List<String> validate(IngestJob job) {
        List<String> violations = new ArrayList<>();
        if (job == null) {
            violations.add("job is required");
            return violations;
        }
        validateSource(job, violations);
        validatePayload(job.payload(), violations);
        validateContentHash(job, violations);
        if (!targetProfileRegistry.isKnown(job.targetProfile())) {
            violations.add("targetProfile is unknown");
        }
        if (job.kind() == null || job.kind().isBlank()) {
            violations.add("kind is required");
        } else if (!DOCUMENT_KINDS.contains(job.kind())) {
            violations.add("kind is unknown");
        }
        return violations;
    }

    private void validateSource(IngestJob job, List<String> violations) {
        if (job.source() == null || job.source().isEmpty()) {
            violations.add("source is required");
            return;
        }
        if (job.source().getOrDefault("provider", "").isBlank()) {
            violations.add("source.provider is required");
        }
        if (job.source().getOrDefault("project", "").isBlank()) {
            violations.add("source.project is required");
        }
    }

    private void validatePayload(DocumentPayload payload, List<String> violations) {
        if (payload == null) {
            violations.add("payload is required");
            return;
        }
        String payloadKind = payload.kind();
        if ("private_locator".equals(payloadKind)) {
            violations.add("private_locator is not a valid queue payload");
        }
        if (payloadKind != null && payloadKind.startsWith("ragflow_")) {
            violations.add("payload.kind must be target-neutral");
        }
        if (!INLINE_PAYLOAD_KIND.equals(payloadKind)) {
            violations.add("payload.kind must be redacted_rag_ready_document for MVP");
        }
        if (!REDACTION_VERSION.equals(payload.redactionVersion())) {
            violations.add("payload.redactionVersion must be redaction.v2");
        }
        if (payload.body() == null || payload.body().isBlank()) {
            violations.add("payload.document.body is required");
        }
    }

    private void validateContentHash(IngestJob job, List<String> violations) {
        if (!ContentHashVerifier.hasCanonicalShape(job.contentHash())) {
            violations.add("contentHash must be sha256:<64 lowercase hex chars>");
            return;
        }
        if (job.payload() != null && !ContentHashVerifier.matches(job.payload().body(), job.contentHash())) {
            violations.add("contentHash mismatch");
        }
    }
}
