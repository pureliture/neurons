package com.local.ragingressqueue.api.dto;

import java.util.List;

public record EnqueueResponse(
    boolean accepted,
    String jobId,
    String status,
    List<String> errors
) {
    public static EnqueueResponse queued(String jobId) {
        return new EnqueueResponse(true, jobId, "queued", List.of());
    }

    public static EnqueueResponse rejected(String status, List<String> errors) {
        return new EnqueueResponse(false, null, status, errors);
    }
}
