package com.local.ragingressqueue.adapter.infra.nats;

import com.local.ragingressqueue.ingest.domain.IngestJob;

public record IngestMessage(IngestJob job, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
    public IngestMessage(IngestJob job, int deliveryAttempt) {
        this(job, deliveryAttempt, AcknowledgementHandle.NOOP);
    }
}
