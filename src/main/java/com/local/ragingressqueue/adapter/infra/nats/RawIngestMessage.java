package com.local.ragingressqueue.adapter.infra.nats;

public record RawIngestMessage(byte[] payload, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
}
