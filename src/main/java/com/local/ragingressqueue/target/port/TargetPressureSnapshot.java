package com.local.ragingressqueue.target.port;

import com.local.ragingressqueue.delivery.domain.TargetPressure;

public record TargetPressureSnapshot(
    TargetPressure pressure,
    int running,
    int unstart,
    int sampled,
    String reason
) {
    public static TargetPressureSnapshot closed(String reason) {
        return new TargetPressureSnapshot(TargetPressure.CLOSED, 0, 0, 0, reason);
    }
}
