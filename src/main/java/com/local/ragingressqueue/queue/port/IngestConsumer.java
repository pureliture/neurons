package com.local.ragingressqueue.queue.port;

import com.local.ragingressqueue.queue.port.IngestMessage;
import java.util.List;

public interface IngestConsumer {
    List<IngestMessage> fetch(int maxBatchSize);

    void ack(IngestMessage message);

    void nak(IngestMessage message);
}
