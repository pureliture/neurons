package com.local.ragingressqueue.queue.port;

public interface AcknowledgementHandle {
    AcknowledgementHandle NOOP = new AcknowledgementHandle() {
        @Override
        public void ack() {
        }

        @Override
        public void nak() {
        }
    };

    void ack();

    void nak();
}
