package com.local.ragingressqueue.queue.port;

import com.local.ragingressqueue.ingest.domain.IngestJob;

public record IngestMessage(IngestJob job, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
    public IngestMessage(IngestJob job, int deliveryAttempt) {
        this(job, deliveryAttempt, AcknowledgementHandle.NOOP);
    }
}
