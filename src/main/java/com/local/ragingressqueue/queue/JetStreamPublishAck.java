package com.local.ragingressqueue.queue;

public record JetStreamPublishAck(String stream, long sequence, boolean duplicate) {
    public boolean persisted() {
        return stream != null && !stream.isBlank() && sequence > 0;
    }
}
