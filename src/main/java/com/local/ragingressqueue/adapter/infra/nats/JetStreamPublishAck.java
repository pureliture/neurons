package com.local.ragingressqueue.adapter.infra.nats;

public record JetStreamPublishAck(String stream, long sequence, boolean duplicate) {
    public boolean persisted() {
        return stream != null && !stream.isBlank() && sequence > 0;
    }
}
