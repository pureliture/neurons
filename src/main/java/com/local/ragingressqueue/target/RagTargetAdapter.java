package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.TargetPressure;

public interface RagTargetAdapter {
    TargetPressure checkPressure(String targetProfile);

    DeliveryResult deliver(IngestJob job, String targetProfile);

    TargetStatusSnapshot getStatus(IngestJob job, String targetProfile);
}
