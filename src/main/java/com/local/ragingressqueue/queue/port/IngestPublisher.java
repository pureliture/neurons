package com.local.ragingressqueue.queue.port;

import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.queue.port.PublishResult;

public interface IngestPublisher {
    PublishResult publish(IngestJob job);
}
