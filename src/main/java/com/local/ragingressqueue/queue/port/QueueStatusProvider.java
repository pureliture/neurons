package com.local.ragingressqueue.queue.port;

public interface QueueStatusProvider {
    QueueStatusSnapshot currentStatus();
}
