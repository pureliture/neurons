package com.local.ragingressqueue.queue;

import com.local.ragingressqueue.core.IngestJob;

public interface IngestPublisher {
    PublishResult publish(IngestJob job);
}
