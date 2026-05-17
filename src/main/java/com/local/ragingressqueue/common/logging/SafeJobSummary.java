package com.local.ragingressqueue.common.logging;
import com.local.ragingressqueue.ingest.domain.IngestJob;

import com.local.ragingressqueue.ingest.domain.validation.RedactionGuard;

public record SafeJobSummary(
    String contentHashPrefix,
    String provider,
    String project,
    String targetProfile,
    String kind,
    String contentType,
    String status
) {
    private static final RedactionGuard REDACTION_GUARD = new RedactionGuard();

    public static SafeJobSummary from(IngestJob job, String status) {
        String provider = safeSlug(job.source() == null ? "unknown" : job.source().getOrDefault("provider", "unknown"));
        String project = safeSlug(job.source() == null ? "unknown" : job.source().getOrDefault("project", "unknown"));
        String contentType = safeContentType(job.payload() == null ? "unknown" : job.payload().contentType());
        return new SafeJobSummary(
            job.contentHashPrefix(),
            provider,
            project,
            safeSlug(job.targetProfile()),
            safeSlug(job.kind()),
            contentType,
            safeSlug(status)
        );
    }

    private static String safeSlug(String value) {
        if (value == null || !REDACTION_GUARD.inspectValue("summary", value).isEmpty()) {
            return "redacted";
        }
        if (!value.matches("[A-Za-z0-9._:-]+")) {
            return "redacted";
        }
        return value;
    }

    private static String safeContentType(String value) {
        if (value == null || !REDACTION_GUARD.inspectValue("summary.contentType", value).isEmpty()) {
            return "redacted";
        }
        if (!value.matches("[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")) {
            return "redacted";
        }
        return value;
    }
}
