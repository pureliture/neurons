package com.local.ragingressqueue.adapter.infra.nats;

import io.nats.client.JetStream;
import io.nats.client.JetStreamApiException;
import io.nats.client.api.PublishAck;
import io.nats.client.impl.Headers;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.io.IOException;

@Component
@Profile("api")
public class NatsJetStreamPublisherGateway implements JetStreamPublisherGateway {
    private static final String NATS_MESSAGE_ID_HEADER = "Nats-Msg-Id";

    private final JetStream jetStream;

    public NatsJetStreamPublisherGateway(JetStream jetStream) {
        this.jetStream = jetStream;
    }

    @Override
    public JetStreamPublishAck publish(String subject, byte[] payload, String messageId) {
        Headers headers = new Headers();
        headers.put(NATS_MESSAGE_ID_HEADER, messageId);
        try {
            PublishAck ack = jetStream.publish(subject, headers, payload);
            return new JetStreamPublishAck(ack.getStream(), ack.getSeqno(), ack.isDuplicate());
        } catch (IOException | JetStreamApiException error) {
            throw new IllegalStateException("nats publish failed: " + error.getMessage(), error);
        }
    }
}
