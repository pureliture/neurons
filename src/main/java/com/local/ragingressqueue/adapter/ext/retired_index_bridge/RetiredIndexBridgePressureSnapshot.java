package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

public record RetiredIndexBridgePressureSnapshot(
    int running,
    int unstart,
    int failed,
    int done,
    int sampled,
    int total
) {
    public RetiredIndexBridgePressureSnapshot(int running, int unstart, int failed, int done, int sampled) {
        this(running, unstart, failed, done, sampled, sampled);
    }
}
