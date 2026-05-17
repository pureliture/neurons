package com.local.ragingressqueue.queue;

import com.local.ragingressqueue.core.IngestJob;

public record IngestMessage(IngestJob job, int deliveryAttempt, AcknowledgementHandle acknowledgementHandle) {
    public IngestMessage(IngestJob job, int deliveryAttempt) {
        this(job, deliveryAttempt, AcknowledgementHandle.NOOP);
    }
}
