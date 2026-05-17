package com.local.ragingressqueue.core;

public enum TargetIndexingState {
    ACCEPTED,
    DELIVERED,
    INDEXING,
    INDEXED,
    FAILED,
    THROTTLED
}
