package com.local.ragingressqueue.queue.port;

public record PublishResult(boolean accepted, String jobId, String error) {
    public static PublishResult accepted(String jobId) {
        return new PublishResult(true, jobId, null);
    }

    public static PublishResult failed(String error) {
        return new PublishResult(false, null, error);
    }
}
