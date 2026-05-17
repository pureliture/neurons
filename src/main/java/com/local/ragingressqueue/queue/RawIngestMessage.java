package com.local.ragingressqueue.queue;

public record RawIngestMessage(byte[] payload, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
}
