package com.local.ragingressqueue.adapter.infra.nats;

public interface QueueStatusProvider {
    QueueStatusSnapshot currentStatus();
}
