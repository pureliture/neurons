package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import com.local.ragingressqueue.delivery.domain.TargetPressure;

public record RetiredIndexBridgePressurePolicy(
    int runningThrottleThreshold,
    int unstartThrottleThreshold,
    int runningClosedThreshold,
    int unstartClosedThreshold
) {
    public static final RetiredIndexBridgePressurePolicy DEFAULT = new RetiredIndexBridgePressurePolicy(20, 5, 100, 25);

    public TargetPressure evaluate(RetiredIndexBridgePressureSnapshot snapshot) {
        if (snapshot.running() >= runningClosedThreshold || snapshot.unstart() >= unstartClosedThreshold) {
            return TargetPressure.CLOSED;
        }
        if (snapshot.running() >= runningThrottleThreshold || snapshot.unstart() >= unstartThrottleThreshold) {
            return TargetPressure.THROTTLED;
        }
        return TargetPressure.OPEN;
    }
}
