package com.local.ragingressqueue.queue;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.util.List;

@Component
@Profile("worker")
public class NatsIngestConsumer implements IngestConsumer {
    private final JetStreamConsumerGateway gateway;
    private final IngestJobMessageCodec codec;

    public NatsIngestConsumer(JetStreamConsumerGateway gateway, IngestJobMessageCodec codec) {
        this.gateway = gateway;
        this.codec = codec;
    }

    @Override
    public List<IngestMessage> fetch(int maxBatchSize) {
        return gateway.fetch(maxBatchSize).stream()
            .map(message -> new IngestMessage(
                codec.decode(message.payload()),
                message.deliveryAttempt(),
                message.acknowledgementHandle()
            ))
            .toList();
    }

    @Override
    public void ack(IngestMessage message) {
        message.acknowledgementHandle().ack();
    }

    @Override
    public void nak(IngestMessage message) {
        message.acknowledgementHandle().nak();
    }
}
