package com.local.ragingressqueue.target;

public record RagFlowPressureSnapshot(
    int running,
    int unstart,
    int failed,
    int done,
    int sampled,
    int total
) {
    public RagFlowPressureSnapshot(int running, int unstart, int failed, int done, int sampled) {
        this(running, unstart, failed, done, sampled, sampled);
    }
}
