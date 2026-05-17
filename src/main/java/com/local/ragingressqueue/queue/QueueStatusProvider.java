package com.local.ragingressqueue.queue;

public interface QueueStatusProvider {
    QueueStatusSnapshot currentStatus();
}
