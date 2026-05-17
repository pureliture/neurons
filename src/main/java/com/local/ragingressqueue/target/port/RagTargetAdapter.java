package com.local.ragingressqueue.target.port;

import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;

public interface RagTargetAdapter {
    TargetPressureSnapshot pressureSnapshot(String targetProfile);

    DeliveryResult deliver(IngestJob job, String targetProfile);

    TargetStatusSnapshot getStatus(IngestJob job, String targetProfile);
}
