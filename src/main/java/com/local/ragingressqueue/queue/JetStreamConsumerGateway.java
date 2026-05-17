package com.local.ragingressqueue.queue;

import java.util.List;

public interface JetStreamConsumerGateway {
    List<RawIngestMessage> fetch(int maxBatchSize);
}
