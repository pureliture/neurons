package com.local.ragingressqueue.target.port;

import com.local.ragingressqueue.common.TargetIndexingState;

public record TargetStatusSnapshot(
    String jobId,
    String contentHash,
    String targetProfile,
    TargetIndexingState status,
    String redactedTargetRef
) {
}
