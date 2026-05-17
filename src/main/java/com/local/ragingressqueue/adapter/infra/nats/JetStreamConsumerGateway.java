package com.local.ragingressqueue.adapter.infra.nats;

import java.util.List;

public interface JetStreamConsumerGateway {
    List<RawIngestMessage> fetch(int maxBatchSize);
}
