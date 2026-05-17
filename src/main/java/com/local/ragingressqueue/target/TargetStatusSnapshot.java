package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.TargetIndexingState;

public record TargetStatusSnapshot(
    String jobId,
    String contentHash,
    String targetProfile,
    TargetIndexingState status,
    String redactedTargetRef
) {
}
