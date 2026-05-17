package com.local.ragingressqueue.worker;

public record DeliveryDecision(Status status, String detail) {
    public enum Status {
        DELIVERED,
        RETRY_SCHEDULED,
        QUARANTINE_CANDIDATE,
        SKIPPED_PRESSURE,
        NO_WORK
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
