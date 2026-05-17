package com.local.ragingressqueue.adapter.infra.nats;

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
