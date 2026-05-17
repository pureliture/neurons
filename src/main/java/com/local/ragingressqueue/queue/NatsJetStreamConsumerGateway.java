package com.local.ragingressqueue.queue;

import io.nats.client.JetStream;
import io.nats.client.JetStreamApiException;
import io.nats.client.JetStreamSubscription;
import io.nats.client.Message;
import io.nats.client.PullSubscribeOptions;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.time.Duration;
import java.util.List;

@Component
@Profile("worker")
public class NatsJetStreamConsumerGateway implements JetStreamConsumerGateway {
    private static final String INGEST_SUBJECTS = "rag.ingress.>";

    private final JetStream jetStream;
    private final String streamName;
    private final String consumerName;
    private final Duration fetchWait;
    private JetStreamSubscription subscription;

    public NatsJetStreamConsumerGateway(
        JetStream jetStream,
        @Value("${rag-ingress.nats.stream}") String streamName,
        @Value("${rag-ingress.nats.consumer}") String consumerName,
        @Value("${rag-ingress.nats.fetch-wait-ms:1000}") long fetchWaitMillis
    ) {
        this.jetStream = jetStream;
        this.streamName = streamName;
        this.consumerName = consumerName;
        this.fetchWait = Duration.ofMillis(fetchWaitMillis);
    }

    @Override
    public List<RawIngestMessage> fetch(int maxBatchSize) {
        return subscription().fetch(maxBatchSize, fetchWait).stream()
            .map(this::toRawIngestMessage)
            .toList();
    }

    private RawIngestMessage toRawIngestMessage(Message message) {
        int deliveredCount = Math.toIntExact(message.metaData().deliveredCount());
        return new RawIngestMessage(message.getData(), deliveredCount, new NatsAcknowledgementHandle(message));
    }

    private JetStreamSubscription subscription() {
        if (subscription == null) {
            subscription = subscribe();
        }
        return subscription;
    }

    private JetStreamSubscription subscribe() {
        PullSubscribeOptions options = PullSubscribeOptions.builder()
            .stream(streamName)
            .durable(consumerName)
            .build();
        try {
            return jetStream.subscribe(INGEST_SUBJECTS, options);
        } catch (IOException | JetStreamApiException error) {
            throw new IllegalStateException("failed to subscribe NATS JetStream consumer", error);
        }
    }

    private static final class NatsAcknowledgementHandle implements AcknowledgementHandle {
        private final Message message;

        private NatsAcknowledgementHandle(Message message) {
            this.message = message;
        }

        @Override
        public void ack() {
            message.ack();
        }

        @Override
        public void nak() {
            message.nak();
        }
    }
}
