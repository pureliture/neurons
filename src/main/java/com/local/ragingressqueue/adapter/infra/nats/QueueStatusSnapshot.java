package com.local.ragingressqueue.adapter.infra.nats;

public record QueueStatusSnapshot(
    long pending,
    long inFlight,
    long redelivered,
    long deadLetter
) {
    public static QueueStatusSnapshot unavailable() {
        return new QueueStatusSnapshot(0, 0, 0, 0);
    }
}
