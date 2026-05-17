package com.local.ragingressqueue.adapter.infra.nats;

public interface JetStreamPublisherGateway {
    JetStreamPublishAck publish(String subject, byte[] payload, String messageId);
}
