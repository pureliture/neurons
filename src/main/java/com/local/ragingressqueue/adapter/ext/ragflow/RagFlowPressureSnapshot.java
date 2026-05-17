package com.local.ragingressqueue.adapter.ext.ragflow;

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
