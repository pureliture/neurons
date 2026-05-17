package com.local.ragingressqueue.queue;

public interface JetStreamPublisherGateway {
    JetStreamPublishAck publish(String subject, byte[] payload, String messageId);
}
