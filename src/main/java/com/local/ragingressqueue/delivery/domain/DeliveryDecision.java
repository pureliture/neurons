package com.local.ragingressqueue.delivery.domain;

import com.local.ragingressqueue.common.IngestStatus;

public record DeliveryDecision(Status status, String detail) {
    public enum Status {
        DELIVERED,
        RETRY_SCHEDULED,
        QUARANTINE_CANDIDATE,
        SKIPPED_PRESSURE,
        NO_WORK
    }

    /**
     * Projects a worker delivery outcome onto the backend-neutral {@link IngestStatus}.
     * DELIVERED is IN_FLIGHT because backend indexing is asynchronous; exhausted retries
     * (QUARANTINE_CANDIDATE) are the terminal DEAD_LETTER signal.
     */
    public IngestStatus toIngestStatus() {
        return switch (status) {
            case DELIVERED -> IngestStatus.IN_FLIGHT;
            case SKIPPED_PRESSURE, NO_WORK, RETRY_SCHEDULED -> IngestStatus.QUEUED;
            case QUARANTINE_CANDIDATE -> IngestStatus.DEAD_LETTER;
        };
    }

    public static DeliveryDecision delivered() {
        return new DeliveryDecision(Status.DELIVERED, "delivered");
    }

    public static DeliveryDecision retryScheduled(String detail) {
        return new DeliveryDecision(Status.RETRY_SCHEDULED, detail);
    }

    public static DeliveryDecision quarantineCandidate(String detail) {
        return new DeliveryDecision(Status.QUARANTINE_CANDIDATE, detail);
    }

    public static DeliveryDecision skippedPressure(String detail) {
        return new DeliveryDecision(Status.SKIPPED_PRESSURE, detail);
    }

    public static DeliveryDecision noWork() {
        return new DeliveryDecision(Status.NO_WORK, "no work");
    }
}
