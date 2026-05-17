package com.local.ragingressqueue.queue.port;

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
