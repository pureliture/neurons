package com.local.ragingressqueue.adapter.infra.nats;

import com.local.ragingressqueue.queue.port.AcknowledgementHandle;

public record RawIngestMessage(byte[] payload, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
}
