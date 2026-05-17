package com.local.ragingressqueue.adapter.infra.nats;

import com.local.ragingressqueue.queue.port.QueueStatusProvider;
import com.local.ragingressqueue.queue.port.QueueStatusSnapshot;
import io.nats.client.JetStreamApiException;
import io.nats.client.JetStreamManagement;
import io.nats.client.api.ConsumerInfo;
import io.nats.client.api.StreamInfo;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.io.IOException;

@Component
@Profile("api")
public class NatsQueueStatusProvider implements QueueStatusProvider {
    private final JetStreamManagement management;
    private final String streamName;
    private final String consumerName;

    public NatsQueueStatusProvider(
        JetStreamManagement management,
        @Value("${rag-ingress.nats.stream}") String streamName,
        @Value("${rag-ingress.nats.consumer}") String consumerName
    ) {
        this.management = management;
        this.streamName = streamName;
        this.consumerName = consumerName;
    }

    @Override
    public QueueStatusSnapshot currentStatus() {
        try {
            StreamInfo streamInfo = management.getStreamInfo(streamName);
            ConsumerInfo consumerInfo = management.getConsumerInfo(streamName, consumerName);
            return new QueueStatusSnapshot(
                streamInfo.getStreamState().getMsgCount(),
                consumerInfo.getNumAckPending(),
                consumerInfo.getRedelivered(),
                0
            );
        } catch (IOException | JetStreamApiException | RuntimeException error) {
            return QueueStatusSnapshot.unavailable();
        }
    }
}
