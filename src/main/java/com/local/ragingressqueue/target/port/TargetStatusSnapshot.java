package com.local.ragingressqueue.target.port;

import com.local.ragingressqueue.common.IngestStatus;

public record TargetStatusSnapshot(
    String jobId,
    String contentHash,
    String targetProfile,
    IngestStatus status,
    String redactedTargetRef
) {
}
