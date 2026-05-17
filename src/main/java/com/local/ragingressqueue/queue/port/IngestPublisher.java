package com.local.ragingressqueue.queue.port;

import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.adapter.infra.nats.PublishResult;

public interface IngestPublisher {
    PublishResult publish(IngestJob job);
}
